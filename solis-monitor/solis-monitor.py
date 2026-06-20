#!/usr/bin/env python3
"""
solis-monitor.py – Modbus TCP poller for Solis inverters.
Outputs human-readable or Prometheus-format metrics via Jinja2 templates.

Improvements over v1:
* Template-based rendering (./templates/)
* Load-percentage for Inverter Output / Grid / Backup vs inverter_power_kw
* Serial verification from config.cfg (mismatch → error, no data)
* Per-value sanity ranges from config.cfg (invalid Modbus data is dropped)

Improvements over v2 (reliability):
* Per-block retry loop (block_retries=3) before raising
* read_range_adaptive() used as graceful fallback when all retries fail
* All-zeros block guard: suspicious all-zero blocks are re-read once
* Whole-poll retry (up to MAX_POLL_ATTEMPTS=3) when too many None values
* Inter-block delay to avoid overwhelming inverter Modbus buffer
* Explicit reconnect with one retry on initial connection failure
"""

import argparse
import configparser
import json
import math
import re
import sys
import time
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pymodbus.client import ModbusTcpClient

CONFIG_FILE = "config.cfg"
DEFAULTS_FILE = "default_sanity_ranges.cfg"
SECTION = "SolisInverter"
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# ── Storage settings register layout ─────────────────────────────────────────
STORAGE_BLOCK1_START = 43024   # battery reserve %
STORAGE_BLOCK1_COUNT = 87      # 43024-43110: reserve % + mode bitmask
STORAGE_BLOCK2_START = 43483   # hybrid function control (bit 3 = allow export inv, bit 7 = peak shaving enable)
STORAGE_BLOCK2_COUNT = 6       # 43483-43488: hybrid ctrl + peak shaving power

# 43110 mode bitmask
STORAGE_MODE_BITS = {
    0:  "Self Use",
    1:  "Time of Use",
    2:  "Off Grid",
    6:  "Feed In Priority",
    11: "Peak Shaving",
}

# ── TOU register layout ──────────────────────────────────────────────────────
TOU_BLOCK_START = 43707   # V2 enable switch; charge slots start at 43708
TOU_BLOCK_COUNT = 85      # 43707-43791: switch + 6 charge slots + 6 discharge slots
TOU_SLOT_FIELDS = 7       # per slot: SOC%, current A, cutoff V, start_h, start_m, end_h, end_m
TOU_CHARGE_START    = 43708
TOU_DISCHARGE_START = 43750

# ── Reliability tunables ─────────────────────────────────────────────────────
BLOCK_RETRIES       = 3      # attempts per POLL_BLOCK before adaptive fallback
BLOCK_RETRY_DELAY   = 3    # seconds between per-block retries
INTER_BLOCK_DELAY   = 0.2   # seconds between successive block reads
ZEROS_REREAD_DELAY  = 0.1    # seconds to wait before re-reading an all-zero block
MAX_POLL_ATTEMPTS   = 3      # whole-poll retries when too many None values
POLL_RETRY_DELAY    = 2.0    # seconds between whole-poll retries
MAX_NONE_THRESHOLD  = 4      # max acceptable None-valued sensors before retrying poll

# ── Register blocks ──────────────────────────────────────────────────────────
POLL_BLOCKS = [
    (33004, 93),  # serial + PV + inverter output + temp/freq/status/battery_temp
    (33116,  6),  # fault codes 01-05 + operating status (Appendix 5 & 6)
    (33133, 47),  # battery + backup + energy counters
    (33251, 14),  # grid meter
    (33580, 17),  # household/backup load energies
]

# Human-readable names for the blocks above (used in config: zeros_ok_<name>)
POLL_BLOCK_NAMES = {
    33004: "main",
    33116: "faults",
    33133: "battery",
    33251: "grid",
    33580: "load",
}

# ── Appendix 2 — full inverter status code map (register 33095) ──────────────
STATUS_MAP = {
    0x0000: "Waiting",
    0x0001: "OpenRun",
    0x0002: "SoftRun",
    0x0003: "Generating",
    0x0004: "Standby",
    0x0005: "StandbySynch",
    0x0006: "GridToLoad",
    0x000F: "Normal",
    0x1004: "Grid Off",
    0x1010: "OV-G-V",        # Grid overvoltage
    0x1011: "UN-G-V",        # Grid undervoltage
    0x1012: "OV-G-F",        # Grid overfreq
    0x1013: "UN-G-F",        # Grid underfreq
    0x1014: "G-IMP/Reve-Grid",
    0x1015: "NO-Grid",
    0x1016: "G-PHASE",
    0x1017: "G-F-FLU",
    0x1018: "OV-G-I",
    0x1019: "IGFOL-F",
    0x1020: "OV-DC",
    0x1021: "OV-BUS",
    0x1022: "UNB-BUS",
    0x1023: "UN-BUS",
    0x1024: "UNB2-BUS",
    0x1025: "OV-DCA-I",
    0x1026: "OV-DCB-I",
    0x1027: "DC-INTF.",
    0x1028: "Reve-DC",
    0x1029: "PvMidIso",
    0x1030: "GRID-INTF.",
    0x1031: "INI-FAULT",
    0x1032: "OV-TEM",
    0x1033: "PV ISO-PRO",
    0x1034: "ILeak-PRO",
    0x1035: "RelayChk-FAIL",
    0x1036: "DSP-B-FAULT",
    0x1037: "DCInj-FAULT",
    0x1038: "12Power-FAULT",
    0x1039: "ILeak-Check",
    0x103A: "UN-TEM",
    0x1040: "AFCI-Check",
    0x1041: "ARC-FAULT",
    0x1042: "RAM-FAULT",
    0x1043: "FLASH-FAULT",
    0x1044: "PC-FAULT",
    0x1045: "REG-FAULT",
    0x1046: "GRID-INTF02",
    0x1047: "IG-AD",
    0x1048: "IGBT-OV-I",
    0x1050: "OV-IgTr",
    0x1051: "OV-Vbatt-H",
    0x1052: "OV-ILLC",
    0x1053: "OV-Vbatt",
    0x1054: "UN-Vbatt",
    0x1055: "NO-Battery",
    0x1056: "OV-VBackup",
    0x1057: "Over-Load",
    0x1058: "DspSelfChk",
    0x2010: "Fail Safe",
    0x2011: "MET_Comm_FAIL",
    0x2012: "CAN_Comm_FAIL",
    0x2014: "DSP_Comm_FAIL",
    0x2015: "Alarm-BMS",
    0x2016: "BatName-FAIL",
    0x2017: "Alarm2-BMS",
    0x2018: "DRM_LINK_FAIL",
    0x2019: "MET_SEL_FAIL",
    0x2020: "HighTemp.AMB",
    0x2021: "LowTemp.AMB",
    0xF010: "Surge Alarm",
    0xF011: "Fan Alarm",
}

STATUS_DESCRIPTION = {
    0x0000: "Normal operation / Waiting",
    0x0001: "Open operating",
    0x0002: "Soft run / Waiting",
    0x0003: "Initializing / Generating",
    0x0004: "Standby",
    0x0005: "Standby synchronize",
    0x0006: "Grid to load",
    0x000F: "Normal running",
    0x1004: "Grid off",
    0x1010: "Grid overvoltage fault",
    0x1011: "Grid undervoltage fault",
    0x1012: "Grid over-frequency fault",
    0x1013: "Grid under-frequency fault",
    0x1014: "Over grid impedance / Grid reverse current",
    0x1015: "No grid detected",
    0x1016: "Unbalanced grid (phase fault)",
    0x1017: "Grid frequency fluctuation",
    0x1018: "Grid overcurrent",
    0x1019: "Grid current sampling error",
    0x1020: "DC overvoltage",
    0x1021: "DC bus overvoltage",
    0x1022: "DC bus unbalanced voltage",
    0x1023: "DC bus undervoltage",
    0x1024: "DC bus unbalanced voltage 2",
    0x1025: "DC channel A overcurrent",
    0x1026: "DC channel B overcurrent",
    0x1027: "DC input interference",
    0x1028: "DC reverse connection",
    0x1029: "PV midpoint grounding fault",
    0x1030: "Grid interference protection",
    0x1031: "DSP initial protection",
    0x1032: "Over temperature protection",
    0x1033: "PV insulation fault",
    0x1034: "Leakage current protection",
    0x1035: "Relay check protection",
    0x1036: "DSP_B protection",
    0x1037: "DC injection protection",
    0x1038: "12V undervoltage fault",
    0x1039: "Leakage current self-check protection",
    0x103A: "Under temperature protection",
    0x1040: "AFCI check fault",
    0x1041: "AFCI arc fault",
    0x1042: "DSP SRAM fault",
    0x1043: "DSP FLASH fault",
    0x1044: "DSP PC pointer fault",
    0x1045: "DSP register fault",
    0x1046: "Grid interference 02 protection",
    0x1047: "Grid current sampling error (AD)",
    0x1048: "IGBT overcurrent",
    0x1050: "Grid transient overcurrent",
    0x1051: "Battery hardware overvoltage fault",
    0x1052: "LLC hardware overcurrent",
    0x1053: "Battery overvoltage",
    0x1054: "Battery undervoltage",
    0x1055: "Battery not connected",
    0x1056: "Backup overvoltage",
    0x1057: "Backup overload",
    0x1058: "DSP self-check error",
    0x2010: "Fail safe activated",
    0x2011: "Meter communication fail",
    0x2012: "Battery (CAN) communication fail",
    0x2014: "DSP communication fail",
    0x2015: "BMS alarm",
    0x2016: "Battery model mismatch",
    0x2017: "BMS alarm 2",
    0x2018: "DRM connection fail",
    0x2019: "Meter selection fail",
    0x2020: "Lead-acid battery high ambient temperature",
    0x2021: "Lead-acid battery low ambient temperature",
    0xF010: "Grid surge warning",
    0xF011: "Fan fault warning",
}

# ── Appendix 5 — Fault registers 33116-33120, bitmask definitions ────────────
FAULT_BIT_MAP = {
    # register 33116 — Grid fault status 01
    33116: [
        "No grid",                    # BIT00
        "Grid overvoltage",           # BIT01
        "Grid undervoltage",          # BIT02
        "Grid over-frequency",        # BIT03
        "Grid under-frequency",       # BIT04
        "Unbalanced grid",            # BIT05
        "Grid frequency fluctuation", # BIT06
        "Grid reverse current",       # BIT07
        "Grid current tracking error",# BIT08
        "Meter COM fail",             # BIT09
        "Fail safe",                  # BIT10
        None, None, None, None, None, # BIT11-15 reserved
    ],
    # register 33117 — Backup load fault status 02
    33117: [
        "Backup overvoltage fault",   # BIT00
        "Backup overload fault",      # BIT01
        None, None, None, None, None, None,
        None, None, None, None, None, None, None, None,
    ],
    # register 33118 — Battery fault status 03
    33118: [
        "Battery not connected",      # BIT00
        "Battery overvoltage check",  # BIT01
        "Battery undervoltage check", # BIT02
        None, None, None, None, None,
        None, None, None, None, None, None, None, None,
    ],
    # register 33119 — Device fault status 04
    33119: [
        "DC overvoltage",             # BIT00
        "DC bus overvoltage",         # BIT01
        "DC bus unbalanced voltage",  # BIT02
        "DC bus undervoltage",        # BIT03
        "DC bus unbalanced voltage 2",# BIT04
        "DC overcurrent A circuit",   # BIT05
        "DC overcurrent B circuit",   # BIT06
        "DC input interference",      # BIT07
        "Grid overcurrent",           # BIT08
        "IGBT overcurrent",           # BIT09
        "Grid interference 02",       # BIT10
        "AFCI self-check",            # BIT11
        "Arc fault (reserved)",       # BIT12
        "Grid current sampling fault",# BIT13
        "DSP self-check error",       # BIT14
        None,                         # BIT15 reserved
    ],
    # register 33120 — Device fault status 05
    33120: [
        "Grid interference",          # BIT00
        "Over DC components",         # BIT01
        "Over temperature protection",# BIT02
        "Relay check protection",     # BIT03
        "Under temperature protection",# BIT04
        "PV insulation fault",        # BIT05
        "12V undervoltage protection",# BIT06
        "Leak current protection",    # BIT07
        "Leak current self-check",    # BIT08
        "DSP initial protection",     # BIT09
        "DSP_B protection",           # BIT10
        "Battery overvoltage hardware fault", # BIT11
        "LLC hardware overcurrent",   # BIT12
        "Grid transient overcurrent", # BIT13
        "CAN COM fail",               # BIT14
        "DSP COM fail",               # BIT15
    ],
}

FAULT_REGISTER_NAMES = {
    33116: "grid",
    33117: "backup",
    33118: "battery",
    33119: "device04",
    33120: "device05",
}

# ── Appendix 6 — Operating status register 33121, bitmask ───────────────────
OP_STATUS_BIT_MAP = [
    "Normal operation",               # BIT00
    "Initializing",                   # BIT01
    "Controlled turn-off",            # BIT02
    "Fault turn-off",                 # BIT03
    "Stand-by",                       # BIT04
    "Limited operation (temp/freq)",  # BIT05
    "Limited operation (external)",   # BIT06
    "Backup overload",                # BIT07
    "Load fault",                     # BIT08
    "Grid fault",                     # BIT09
    "Battery fault",                  # BIT10
    None,                             # BIT11 reserved
    "Grid surge warning",             # BIT12
    "Fan fault warning",              # BIT13
    None, None,                       # BIT14-15 reserved
]

BATTERY_DIR_MAP = {0: "Charging", 1: "Discharging", 2: "Idle"}

# ── Sensor definitions ───────────────────────────────────────────────────────
SENSORS = [
    {"key": "serial",                            "name": "Serial Number",                    "registers": list(range(33004, 33020)), "type": "ascii", "unit": ""},
    {"key": "pv_total_energy_generation",        "name": "PV Total Energy Generation",       "registers": [33029, 33030], "type": "u32",  "unit": "kWh", "scale": 1},
    {"key": "pv_today_energy_generation",        "name": "PV Today Energy Generation",       "registers": [33035],        "type": "u16",  "unit": "kWh", "scale": 0.1},
    {"key": "pv_voltage_1",                      "name": "PV Voltage 1",                     "registers": [33049],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "pv_current_1",                      "name": "PV Current 1",                     "registers": [33050],        "type": "u16",  "unit": "A",   "scale": 0.1},
    {"key": "pv_voltage_2",                      "name": "PV Voltage 2",                     "registers": [33051],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "pv_current_2",                      "name": "PV Current 2",                     "registers": [33052],        "type": "u16",  "unit": "A",   "scale": 0.1},
    {"key": "pv_voltage_3",                      "name": "PV Voltage 3",                     "registers": [33053],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "pv_current_3",                      "name": "PV Current 3",                     "registers": [33054],        "type": "u16",  "unit": "A",   "scale": 0.1},
    {"key": "pv_voltage_4",                      "name": "PV Voltage 4",                     "registers": [33055],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "pv_current_4",                      "name": "PV Current 4",                     "registers": [33056],        "type": "u16",  "unit": "A",   "scale": 0.1},
    {"key": "pv_total_power",                    "name": "Total PV Power",                   "registers": [33057, 33058], "type": "u32",  "unit": "W"},
    {"key": "inverter_voltage_l1",               "name": "Inverter Voltage L1",              "registers": [33073],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "inverter_voltage_l2",               "name": "Inverter Voltage L2",              "registers": [33074],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "inverter_voltage_l3",               "name": "Inverter Voltage L3",              "registers": [33075],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "inverter_current_l1",               "name": "Inverter Current L1",              "registers": [33076],        "type": "u16",  "unit": "A",   "scale": 0.1},
    {"key": "inverter_current_l2",               "name": "Inverter Current L2",              "registers": [33077],        "type": "u16",  "unit": "A",   "scale": 0.1},
    {"key": "inverter_current_l3",               "name": "Inverter Current L3",              "registers": [33078],        "type": "u16",  "unit": "A",   "scale": 0.1},
    {"key": "inverter_active_power",             "name": "Inverter Active Power",            "registers": [33079, 33080], "type": "s32",  "unit": "W"},
    {"key": "inverter_temperature",              "name": "Inverter Temperature",             "registers": [33093],        "type": "u16",  "unit": "C",   "scale": 0.1},
    {"key": "grid_frequency",                    "name": "Grid Frequency",                   "registers": [33094],        "type": "u16",  "unit": "Hz",  "scale": 0.01},
    {"key": "status",                            "name": "Status",                           "registers": [33095],        "type": "status", "unit": ""},
    {"key": "battery_temperature",               "name": "Battery Temperature",              "registers": [33096],        "type": "u16",  "unit": "C",   "scale": 0.1},
    {"key": "battery_voltage",                   "name": "Battery Voltage",                  "registers": [33133],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "battery_current",                   "name": "Battery Current",                  "registers": [33134],        "type": "s16",  "unit": "A",   "scale": 0.1},
    {"key": "battery_direction",                 "name": "Battery Direction",                "registers": [33135],        "type": "battery_dir", "unit": ""},
    {"key": "backup_voltage_l1",                 "name": "Backup Voltage L1",                "registers": [33137],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "backup_current_l1",                 "name": "Backup Current L1",                "registers": [33138],        "type": "u16",  "unit": "A",   "scale": 0.1},
    {"key": "battery_soc",                       "name": "Battery SOC",                      "registers": [33139],        "type": "u16",  "unit": "%"},
    {"key": "battery_soh",                       "name": "Battery SOH",                      "registers": [33140],        "type": "u16",  "unit": "%"},
    {"key": "battery_voltage_bms",               "name": "Battery Voltage (BMS)",            "registers": [33141],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "battery_current_bms",               "name": "Battery Current (BMS)",            "registers": [33142],        "type": "s16",  "unit": "A",   "scale": 0.1},
    {"key": "battery_charge_current_limit_bms",  "name": "BMS Charge Current Limit",         "registers": [33143],        "type": "u16",  "unit": "A",   "scale": 0.1},
    {"key": "battery_discharge_current_limit_bms","name": "BMS Discharge Current Limit",     "registers": [33144],        "type": "u16",  "unit": "A",   "scale": 0.1},
    {"key": "battery_fault_status_1_bms",        "name": "Battery Fault Status 1",           "registers": [33145],        "type": "hex",  "unit": ""},
    {"key": "battery_fault_status_2_bms",        "name": "Battery Fault Status 2",           "registers": [33146],        "type": "hex",  "unit": ""},
    {"key": "household_load_power",              "name": "Household Load Power",             "registers": [33147],        "type": "u16",  "unit": "W"},
    {"key": "backup_load_power",                 "name": "Backup Load Power",                "registers": [33148],        "type": "u16",  "unit": "W"},
    {"key": "battery_power",                     "name": "Battery Power",                    "registers": [33149, 33150], "type": "s32",  "unit": "W"},
    {"key": "ac_grid_port_power",                "name": "AC Grid Port Power",               "registers": [33151, 33152], "type": "s32",  "unit": "W"},
    {"key": "backup_voltage_l2",                 "name": "Backup Voltage L2",                "registers": [33153],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "backup_current_l2",                 "name": "Backup Current L2",                "registers": [33154],        "type": "u16",  "unit": "A",   "scale": 0.1},
    {"key": "backup_voltage_l3",                 "name": "Backup Voltage L3",                "registers": [33155],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "backup_current_l3",                 "name": "Backup Current L3",                "registers": [33156],        "type": "u16",  "unit": "A",   "scale": 0.1},
    {"key": "battery_charge_energy_total",       "name": "Total Battery Charge Energy",      "registers": [33161, 33162], "type": "u32",  "unit": "kWh", "scale": 1},
    {"key": "battery_charge_energy_today",       "name": "Today Battery Charge Energy",      "registers": [33163],        "type": "u16",  "unit": "kWh", "scale": 0.1},
    {"key": "battery_discharge_energy_total",    "name": "Total Battery Discharge Energy",   "registers": [33165, 33166], "type": "u32",  "unit": "kWh", "scale": 1},
    {"key": "battery_discharge_energy_today",    "name": "Today Battery Discharge Energy",   "registers": [33167],        "type": "u16",  "unit": "kWh", "scale": 0.1},
    {"key": "grid_import_energy_total",          "name": "Total Grid Import Energy",         "registers": [33169, 33170], "type": "u32",  "unit": "kWh", "scale": 1},
    {"key": "grid_import_energy_today",          "name": "Today Grid Import Energy",         "registers": [33171],        "type": "u16",  "unit": "kWh", "scale": 0.1},
    {"key": "grid_export_energy_total",          "name": "Total Grid Export Energy",         "registers": [33173, 33174], "type": "u32",  "unit": "kWh", "scale": 1},
    {"key": "grid_export_energy_today",          "name": "Today Grid Export Energy",         "registers": [33175],        "type": "u16",  "unit": "kWh", "scale": 0.1},
    {"key": "load_energy_total",                 "name": "Total Load Energy",                "registers": [33177, 33178], "type": "u32",  "unit": "kWh", "scale": 1},
    {"key": "load_energy_today",                 "name": "Today Load Energy",                "registers": [33179],        "type": "u16",  "unit": "kWh", "scale": 0.1},
    {"key": "grid_voltage_l1",                   "name": "Grid Voltage L1",                  "registers": [33251],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "grid_current_l1",                   "name": "Grid Current L1",                  "registers": [33252],        "type": "u16",  "unit": "A",   "scale": 0.01},
    {"key": "grid_voltage_l2",                   "name": "Grid Voltage L2",                  "registers": [33253],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "grid_current_l2",                   "name": "Grid Current L2",                  "registers": [33254],        "type": "u16",  "unit": "A",   "scale": 0.01},
    {"key": "grid_voltage_l3",                   "name": "Grid Voltage L3",                  "registers": [33255],        "type": "u16",  "unit": "V",   "scale": 0.1},
    {"key": "grid_current_l3",                   "name": "Grid Current L3",                  "registers": [33256],        "type": "u16",  "unit": "A",   "scale": 0.01},
    {"key": "grid_power_l1",                     "name": "Grid Power L1",                    "registers": [33257, 33258], "type": "s32",  "unit": "W"},
    {"key": "grid_power_l2",                     "name": "Grid Power L2",                    "registers": [33259, 33260], "type": "s32",  "unit": "W"},
    {"key": "grid_power_l3",                     "name": "Grid Power L3",                    "registers": [33261, 33262], "type": "s32",  "unit": "W"},
    {"key": "grid_power_total",                  "name": "Grid Total Power",                 "registers": [33263, 33264], "type": "s32",  "unit": "W"},
    {"key": "household_load_energy_total",       "name": "Household Load Total Energy",      "registers": [33580, 33581], "type": "u32",  "unit": "kWh", "scale": 1},
    {"key": "household_load_energy_year",        "name": "Household Load Year Energy",       "registers": [33582, 33583], "type": "u32",  "unit": "kWh", "scale": 1},
    {"key": "household_load_energy_month",       "name": "Household Load Month Energy",      "registers": [33584, 33585], "type": "u32",  "unit": "kWh", "scale": 1},
    {"key": "household_load_energy_today",       "name": "Household Load Today Energy",      "registers": [33586],        "type": "u16",  "unit": "kWh", "scale": 0.1},
    {"key": "backup_load_energy_total",          "name": "Backup Load Total Energy",         "registers": [33590, 33591], "type": "u32",  "unit": "kWh", "scale": 1},
    {"key": "backup_load_energy_year",           "name": "Backup Load Year Energy",          "registers": [33592, 33593], "type": "u32",  "unit": "kWh", "scale": 1},
    {"key": "backup_load_energy_month",          "name": "Backup Load Month Energy",         "registers": [33594, 33595], "type": "u32",  "unit": "kWh", "scale": 1},
    {"key": "backup_load_energy_today",          "name": "Backup Load Today Energy",         "registers": [33596],        "type": "u16",  "unit": "kWh", "scale": 0.1},
    # Fault & status registers (Appendix 5, 6)
    {"key": "fault_01_grid",    "name": "Fault Code 01 (Grid)",             "registers": [33116], "type": "u16", "unit": ""},
    {"key": "fault_02_backup",  "name": "Fault Code 02 (Backup)",           "registers": [33117], "type": "u16", "unit": ""},
    {"key": "fault_03_battery", "name": "Fault Code 03 (Battery)",          "registers": [33118], "type": "u16", "unit": ""},
    {"key": "fault_04_device",  "name": "Fault Code 04 (Device)",           "registers": [33119], "type": "u16", "unit": ""},
    {"key": "fault_05_device",  "name": "Fault Code 05 (Device)",           "registers": [33120], "type": "u16", "unit": ""},
    {"key": "op_status",        "name": "Operating Status (Appendix 6)",    "registers": [33121], "type": "u16", "unit": ""},
]

# ── Range map: key-prefix → config keys ─────────────────────────────────────
RANGE_MAP = [
    (re.compile(r"^pv_voltage_"),                  "pv_voltage_min",      "pv_voltage_max"),
    (re.compile(r"^(inverter|grid|backup)_voltage_"), "ac_voltage_min",   "ac_voltage_max"),
    (re.compile(r"^battery_voltage"),              "battery_voltage_min", "battery_voltage_max"),
    (re.compile(r"^pv_current_"),                  "pv_current_min",      "pv_current_max"),
    (re.compile(r"^(inverter|grid|backup)_current_"), "ac_current_min",   "ac_current_max"),
    (re.compile(r"^battery_current"),              "battery_current_min", "battery_current_max"),
    (re.compile(r"^battery_s[oc]"),                "pct_min",             "pct_max"),
    (re.compile(r"_power_"),                       "power_w_min",         "power_w_max"),
    (re.compile(r"_temperature"),                  "temperature_min",     "temperature_max"),
    (re.compile(r"_frequency"),                    "frequency_min",       "frequency_max"),
]

# ── Config ───────────────────────────────────────────────────────────────────

def load_config():
    script_dir   = Path(__file__).resolve().parent
    cfg_name     = globals().get("CONFIG_FILE", "config.cfg")
    cfg_path     = (script_dir / cfg_name) if not Path(cfg_name).is_absolute() else Path(cfg_name)
    defaults_name  = globals().get("DEFAULTS_FILE", "default_sanity_ranges.cfg")
    defaults_path  = (script_dir / defaults_name) if not Path(defaults_name).is_absolute() else Path(defaults_name)

    cfg = configparser.ConfigParser()
    if not defaults_path.exists():
        raise FileNotFoundError(f"Defaults file not found: {defaults_path}")
    cfg.read(defaults_path, encoding="utf-8")

    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    cfg.read(cfg_path, encoding="utf-8")
    if SECTION not in cfg:
        raise KeyError(f"Missing section [{SECTION}] in {cfg_path}")

    sec = cfg[SECTION]
    ip  = sec.get("inverter_ip")
    if not ip:
        raise ValueError(f"Missing inverter_ip in [{SECTION}]")

    def fget(key, default):
        v = sec.get(key)
        return float(v) if v is not None else default

    ranges = {}
    for pattern, min_key, max_key in RANGE_MAP:
        min_v = fget(min_key, None)
        max_v = fget(max_key, None)
        if min_v is not None and max_v is not None:
            ranges[min_key.replace("_min", "")] = (min_v, max_v)

    zeros_ok = {
        start
        for start, name in POLL_BLOCK_NAMES.items()
        if sec.getboolean(f"zeros_ok_{name}", fallback=False)
    }

    return {
        "ip":             ip,
        "port":           sec.getint("inverter_port", fallback=502),
        "slave_id":       sec.getint("slave_id", fallback=1),
        "use_zero_based": sec.getboolean("use_zero_based_addressing", fallback=False),
        "brand":          "solis",
        "config_path":    str(cfg_path),
        "expected_serial": sec.get("serial", "").strip(),
        "inverter_power_w": fget("inverter_power_kw", 30.0) * 1000.0,
        "mppt_count":       sec.getint("mppt_count", fallback=4),
        "ranges":         ranges,
        "zeros_ok":       zeros_ok,
        "_raw_sec":       sec,
    }

# ── Decode helpers ───────────────────────────────────────────────────────────

def u16(v): return v
def s16(v): return v - 65536 if v > 32767 else v
def u32(hi, lo): return (hi << 16) | lo
def s32(hi, lo):
    value = (hi << 16) | lo
    return value - 4294967296 if value > 2147483647 else value

def decode_ascii(registers):
    chars = []
    for reg in registers:
        chars.append(chr((reg >> 8) & 0xFF))
        chars.append(chr(reg & 0xFF))
    return "".join(c for c in chars if 32 <= ord(c) <= 126).strip()

def decode_value(sensor, raw):
    t = sensor["type"]
    if   t == "ascii":       value = decode_ascii(raw)
    elif t == "u16":         value = u16(raw[0])
    elif t == "s16":         value = s16(raw[0])
    elif t == "u32":         value = u32(raw[0], raw[1])
    elif t == "s32":         value = s32(raw[0], raw[1])
    elif t == "hex":         value = f"0x{raw[0]:04X}"
    elif t == "status":      value = STATUS_MAP.get(raw[0], f"0x{raw[0]:04X}")
    elif t == "battery_dir": value = BATTERY_DIR_MAP.get(raw[0], f"Unknown ({raw[0]})")
    else:                    value = raw[0]

    scale = sensor.get("scale")
    if scale and isinstance(value, (int, float)):
        value = value * scale
    return value

# ── Range validation ─────────────────────────────────────────────────────────

def _range_for_key(key, cfg):
    sec = cfg.get("_raw_sec")
    if sec is None:
        return None
    for pattern, min_cfg, max_cfg in RANGE_MAP:
        if pattern.search(key):
            try:
                return (float(sec[min_cfg]), float(sec[max_cfg]))
            except (KeyError, ValueError):
                return None
    return None

def in_range(key, value, cfg):
    if not isinstance(value, (int, float)):
        return True
    r = _range_for_key(key, cfg)
    if r is None:
        return True
    lo, hi = r
    return lo <= value <= hi

# ── Modbus read (reliability layer) ─────────────────────────────────────────

def _store_block(regmap, start, registers):
    for i, value in enumerate(registers):
        regmap[start + i] = value

def _is_block_all_zeros(regmap, start, count):
    """True when every register in a block is 0 – likely a transient inverter response."""
    return all(regmap.get(start + i, 1) == 0 for i in range(count))

def read_range_adaptive(client, start, count, slave_id, use_zero_based, regmap):
    """Recursively bisect a register range, storing whatever the inverter returns."""
    address = start - 1 if use_zero_based else start
    rr = client.read_input_registers(address=address, count=count, device_id=slave_id)
    if not rr.isError():
        _store_block(regmap, start, rr.registers)
        return
    if count == 1:
        return  # single register failed – leave it absent from regmap
    left  = count // 2
    right = count - left
    read_range_adaptive(client, start,        left,  slave_id, use_zero_based, regmap)
    read_range_adaptive(client, start + left, right, slave_id, use_zero_based, regmap)

def read_registers(client, cfg):
    """
    Read all POLL_BLOCKS with per-block retries and an adaptive fallback.
    Also guards against all-zero blocks by re-reading once after a short delay.
    Blocks listed in cfg["zeros_ok"] skip the all-zeros guard entirely.
    """
    regmap = {}
    slave_id       = cfg["slave_id"]
    use_zero_based = cfg["use_zero_based"]
    zeros_ok       = cfg.get("zeros_ok", set())

    for start, count in POLL_BLOCKS:
        address = start - 1 if use_zero_based else start
        success = False

        for attempt in range(BLOCK_RETRIES):
            rr = client.read_input_registers(
                address=address, count=count, device_id=slave_id
            )
            if not rr.isError():
                _store_block(regmap, start, rr.registers)
                success = True
                break
            print(
                f"#WARN: block {start}-{start+count-1} attempt {attempt+1}/{BLOCK_RETRIES} "
                f"failed: {rr}", file=sys.stderr
            )
            if attempt < BLOCK_RETRIES - 1:
                time.sleep(BLOCK_RETRY_DELAY)

        if not success:
            # Graceful degradation: split the block and read what we can
            print(
                f"#WARN: block {start}-{start+count-1} all retries failed, "
                "switching to adaptive read", file=sys.stderr
            )
            read_range_adaptive(client, start, count, slave_id, use_zero_based, regmap)

        # All-zeros guard: Solis sometimes returns 0x0000 for every register
        # during brief internal transitions even though the read succeeds.
        # Skipped when zeros_ok_<name> = true in config (e.g. grid is physically off).
        elif _is_block_all_zeros(regmap, start, count):
            block_name = POLL_BLOCK_NAMES.get(start, str(start))
            if start in zeros_ok:
                print(
                    f"#INFO: block {start}-{start+count-1} ({block_name}) all zeros – "
                    "accepted, skipping re-read", file=sys.stderr
                )
            else:
                print(
                    f"#WARN: block {start}-{start+count-1} returned all zeros – "
                    f"re-reading after {ZEROS_REREAD_DELAY}s", file=sys.stderr
                )
                time.sleep(ZEROS_REREAD_DELAY)
                rr2 = client.read_input_registers(
                    address=address, count=count, device_id=slave_id
                )
                if not rr2.isError():
                    _store_block(regmap, start, rr2.registers)

        time.sleep(INTER_BLOCK_DELAY)

    return regmap

# ── TOU (holding registers, FC3) ─────────────────────────────────────────────

def read_tou_registers(client, cfg):
    return _read_holding_block(client, cfg, TOU_BLOCK_START, TOU_BLOCK_COUNT, "TOU block")

def _decode_tou_slot(regmap, base, enabled):
    soc_pct   = regmap.get(base)
    current_a = regmap.get(base + 1)
    cutoff_v  = regmap.get(base + 2)
    start_h   = regmap.get(base + 3, 0)
    start_m   = regmap.get(base + 4, 0)
    end_h     = regmap.get(base + 5, 0)
    end_m     = regmap.get(base + 6, 0)
    return {
        "enabled":   enabled,
        "soc_pct":   soc_pct   if soc_pct   is not None else 0,
        "current_a": (current_a * 0.1) if current_a is not None else 0.0,
        "cutoff_v":  (cutoff_v * 0.1) if cutoff_v is not None else 0.0,
        "start":     f"{start_h:02d}:{start_m:02d}",
        "end":       f"{end_h:02d}:{end_m:02d}",
    }

def decode_tou(regmap):
    """Decode TOU V2 registers. Returns None if the block was not read.
    Register 43707 is a bitmask: bits 0-5 = charge slots 1-6, bits 6-11 = discharge slots 1-6.
    """
    if TOU_BLOCK_START not in regmap:
        return None
    switch = regmap[TOU_BLOCK_START]
    charge_slots = [
        _decode_tou_slot(regmap, TOU_CHARGE_START + i * TOU_SLOT_FIELDS, bool((switch >> i) & 1))
        for i in range(6)
    ]
    discharge_slots = [
        _decode_tou_slot(regmap, TOU_DISCHARGE_START + i * TOU_SLOT_FIELDS, bool((switch >> (6 + i)) & 1))
        for i in range(6)
    ]
    return {
        "enabled":         bool(switch),
        "charge_slots":    charge_slots,
        "discharge_slots": discharge_slots,
    }

# ── Storage settings (holding registers, FC3) ────────────────────────────────

def _read_holding_block(client, cfg, start, count, label):
    """Read a single holding-register block with retries. Returns partial regmap."""
    slave_id       = cfg["slave_id"]
    use_zero_based = cfg["use_zero_based"]
    address = start - 1 if use_zero_based else start
    for attempt in range(BLOCK_RETRIES):
        rr = client.read_holding_registers(address=address, count=count, device_id=slave_id)
        if not rr.isError():
            regmap = {}
            _store_block(regmap, start, rr.registers)
            return regmap
        print(f"#WARN: {label} attempt {attempt + 1}/{BLOCK_RETRIES} failed: {rr}", file=sys.stderr)
        if attempt < BLOCK_RETRIES - 1:
            time.sleep(BLOCK_RETRY_DELAY)
    print(f"#WARN: {label} unavailable", file=sys.stderr)
    return {}

def read_storage_registers(client, cfg):
    regmap = {}
    regmap.update(_read_holding_block(client, cfg, STORAGE_BLOCK1_START, STORAGE_BLOCK1_COUNT, "storage block1"))
    regmap.update(_read_holding_block(client, cfg, STORAGE_BLOCK2_START, STORAGE_BLOCK2_COUNT, "storage block2"))
    return regmap

def decode_storage(regmap):
    """Decode storage settings. Returns None if key registers are missing."""
    reg43110 = regmap.get(43110)
    reg43483 = regmap.get(43483)
    if reg43110 is None:
        return None

    mode = next(
        (name for bit, name in sorted(STORAGE_MODE_BITS.items()) if (reg43110 >> bit) & 1),
        "Unknown",
    )
    raw_export_limit      = regmap.get(43074)
    raw_peak_shaving_power = regmap.get(43488)
    return {
        "mode":                  mode,
        "battery_reserve_on":    bool((reg43110 >> 4) & 1),
        "battery_reserve_pct":   regmap.get(43024, 0),
        "allow_grid_charge":     bool((reg43110 >> 5) & 1),
        "allow_export":          bool((reg43483 >> 3) & 1) if reg43483 is not None else None,
        "max_export_power_w":    raw_export_limit * 100 if raw_export_limit is not None else None,
        "peak_shaving_on":       bool((reg43483 >> 7) & 1) if reg43483 is not None else None,
        "peak_shaving_power_w":  raw_peak_shaving_power * 100 if raw_peak_shaving_power is not None else None,
    }

def dump_storage_registers(regmap):
    """Print raw storage register values for debugging."""
    print("=== Storage Register Dump ===")
    targets = {
        43024: "battery_reserve_pct",
        43074: "max_export_power   ",
        43110: "mode_bitmask       ",
        43483: "hybrid_func_ctrl   ",
        43488: "peak_shaving_power ",
    }
    for reg, label in sorted(targets.items()):
        val = regmap.get(reg)
        if val is not None:
            print(f"  {reg}  {label} : {val:6d}  (0x{val:04X})")
        else:
            print(f"  {reg}  {label} :    N/A")

    reg43110 = regmap.get(43110)
    if reg43110 is not None:
        print(f"\n  43110 mode bits:")
        for bit, name in sorted(STORAGE_MODE_BITS.items()):
            print(f"    bit {bit:2d} ({name:16s}): {(reg43110 >> bit) & 1}")
        print(f"    bit  4 (battery reserve   ): {(reg43110 >> 4) & 1}")
        print(f"    bit  5 (allow grid charge  ): {(reg43110 >> 5) & 1}")

    reg43074 = regmap.get(43074)
    if reg43074 is not None:
        print(f"\n  43074 max export power: {reg43074 * 100} W  (raw {reg43074})")

    reg43483 = regmap.get(43483)
    if reg43483 is not None:
        print(f"\n  43483 hybrid ctrl bits:")
        print(f"    bit  3 (allow export       ): {(reg43483 >> 3) & 1}")
        print(f"    bit  7 (peak shaving enable): {(reg43483 >> 7) & 1}")

    reg43488 = regmap.get(43488)
    if reg43488 is not None:
        print(f"\n  43488 peak shaving power: {reg43488}  (raw — scale unknown)")

    KNOWN = {43024, 43074, 43110, 43483, 43488}
    print("\n  Full scan (non-zero, unknown registers only):")
    for reg in sorted(regmap):
        if reg in KNOWN:
            continue
        val = regmap[reg]
        if val != 0:
            print(f"    {reg}: {val}  (0x{val:04X})")

# ── Build & validate values ───────────────────────────────────────────────────

def build_values(regmap, cfg):
    """
    Decode all sensors. Values that fail range checks are replaced with None.
    Returns (values_dict, skipped_list).
    """
    values  = {}
    skipped = []

    for sensor in SENSORS:
        regs = sensor["registers"]
        if not all(r in regmap for r in regs):
            skipped.append((sensor["name"], regs, "unavailable"))
            continue

        raw   = [regmap[r] for r in regs]
        value = decode_value(sensor, raw)
        key   = sensor["key"]

        if isinstance(value, (int, float)) and not in_range(key, value, cfg):
            skipped.append((sensor["name"], regs, f"out-of-range ({value})"))
            values[key] = None
        else:
            values[key] = value

    return values, skipped

# ── Serial check ─────────────────────────────────────────────────────────────

def check_serial(values, cfg):
    read_serial = str(values.get("serial", "")).strip()
    exp_serial  = cfg["expected_serial"]
    if not exp_serial:
        return True, read_serial
    return (read_serial == exp_serial), read_serial

# ── Numeric helpers ───────────────────────────────────────────────────────────

def num(v, default=0.0):
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    return default

def fmt1(v): return "NaN" if v is None else f"{num(v):.1f}"
def fmt2(v): return "NaN" if v is None else f"{num(v):.2f}"
def fmt0(v): return "NaN" if v is None else f"{round(num(v)):.0f}"
def pv_power(v, a): return round(num(v) * num(a))

def load_pct(watts, rated_w):
    if watts is None:
        return None
    if rated_w <= 0:
        return 0
    return round(min(100.0, max(0.0, abs(num(watts)) / rated_w * 100.0)), 1)

def power_factor(p_w, apparent_va):
    """P / S clamped to [-1, 1]. Returns None when apparent power is too small to be meaningful."""
    if abs(apparent_va) < 1.0:
        return None
    return max(-1.0, min(1.0, float(p_w) / float(apparent_va)))

def prom_value(v):
    if not isinstance(v, (int, float)) or math.isnan(v):
        return None
    rounded = round(float(v), 1)
    if abs(rounded - round(rounded)) < 1e-9:
        return str(int(round(rounded)))
    return f"{rounded:.1f}"

def hex_to_int(hex_str):
    try:
        return int(str(hex_str), 16)
    except (ValueError, TypeError):
        return 0

def decode_fault_bits(raw_value, bit_map):
    active = []
    for bit, name in enumerate(bit_map):
        if name and (raw_value >> bit) & 1:
            active.append(name)
    return active

def decode_op_status_bits(raw_value):
    return decode_fault_bits(raw_value, OP_STATUS_BIT_MAP)

def faults_to_label(fault_list):
    return ", ".join(fault_list) if fault_list else "OK"

def status_description(code):
    return STATUS_DESCRIPTION.get(code, f"Unknown (0x{code:04X})")

# ── Template context builder ─────────────────────────────────────────────────

def build_context(values, cfg):
    rated_w    = cfg["inverter_power_w"]
    mppt_count = cfg.get("mppt_count", 4)

    pv_strings = []
    for idx in range(1, mppt_count + 1):
        vraw = values.get(f"pv_voltage_{idx}")
        araw = values.get(f"pv_current_{idx}")
        v = num(vraw)
        a = num(araw)
        p = None if (vraw is None or araw is None) else pv_power(v, a)
        pv_strings.append({"name": f"PV{idx}", "voltage": fmt1(vraw), "current": fmt2(araw), "power": fmt0(p)})
    active_pv = sum(1 for s in pv_strings if s["power"] not in (None, "NaN", "0") and num(s["power"]) > 0)

    per_phase_w    = rated_w / 3.0
    inverter_phases = []
    for ph in [1, 2, 3]:
        vraw = values.get(f"inverter_voltage_l{ph}")
        araw = values.get(f"inverter_current_l{ph}")
        v = num(vraw)
        a = num(araw)
        p = None if (vraw is None or araw is None) else round(v * a)
        inverter_phases.append({
            "name": f"L{ph}", "voltage": fmt1(vraw), "current": fmt2(araw), "power": fmt0(p),
            "load_pct": load_pct(p, per_phase_w),
        })

    grid_phases = []
    for ph in [1, 2, 3]:
        v     = num(values.get(f"inverter_voltage_l{ph}"))  # CT has no voltage sense; inverter V == grid V
        a     = num(values.get(f"grid_current_l{ph}"))
        p_raw = num(values.get(f"grid_power_l{ph}"))        # Solis: positive=export, negative=import
        p     = -p_raw                                        # flip to: positive=import, negative=export
        pf    = power_factor(abs(p_raw), abs(v * a))         # always positive magnitude
        grid_phases.append({
            "name": f"L{ph}", "voltage": fmt1(v), "current": fmt2(a), "power": fmt0(p),
            "load_pct": load_pct(p, per_phase_w),
            "power_factor": f"{pf:.3f}" if pf is not None else "N/A",
            "power_factor_num": pf,
        })

    backup_phases = []
    for ph in [1, 2, 3]:
        vraw = values.get(f"backup_voltage_l{ph}")
        araw = values.get(f"backup_current_l{ph}")
        v = num(vraw)
        a = num(araw)
        p = None if (vraw is None or araw is None) else round(v * a)
        backup_phases.append({
            "name": f"L{ph}", "voltage": fmt1(vraw), "current": fmt2(araw), "power": fmt0(p),
            "load_pct": load_pct(p, per_phase_w),
        })

    total_grid  = -num(values.get("grid_power_total"))  # Solis: positive=export → flip to positive=import
    inv_active  = num(values.get("inverter_active_power"))
    backup_total = num(values.get("backup_load_power"))

    inv_total_apparent = sum(
        num(values.get(f"inverter_voltage_l{ph}")) * num(values.get(f"inverter_current_l{ph}"))
        for ph in [1, 2, 3]
    )
    inv_pf = power_factor(inv_active, inv_total_apparent)

    backup_total_apparent = sum(
        num(values.get(f"backup_voltage_l{ph}")) * num(values.get(f"backup_current_l{ph}"))
        for ph in [1, 2, 3]
    )
    backup_pf = power_factor(backup_total, backup_total_apparent)

    f1s = lambda k: fmt1(values.get(k))
    f2s = lambda k: fmt2(values.get(k))
    f0s = lambda k: fmt0(values.get(k))
    n   = lambda k: num(values.get(k))

    fault_regs = {
        "fault_01_grid":    33116,
        "fault_02_backup":  33117,
        "fault_03_battery": 33118,
        "fault_04_device":  33119,
        "fault_05_device":  33120,
    }
    fault_data = {}
    any_fault  = False
    for fkey, freg in fault_regs.items():
        raw_val = values.get(fkey)
        raw_int = int(raw_val) if isinstance(raw_val, (int, float)) and raw_val is not None else 0
        bit_map = FAULT_BIT_MAP.get(freg, [])
        active  = decode_fault_bits(raw_int, bit_map)
        fault_data[fkey] = {
            "raw":   raw_int,
            "hex":   f"0x{raw_int:04X}",
            "active": active,
            "label": faults_to_label(active),
            "name":  FAULT_REGISTER_NAMES.get(freg, fkey),
        }
        if active:
            any_fault = True

    op_raw  = values.get("op_status")
    op_int  = int(op_raw) if isinstance(op_raw, (int, float)) and op_raw is not None else 0
    op_bits = decode_op_status_bits(op_int)
    op_label = faults_to_label(op_bits)

    status_raw      = values.get("status", "")
    status_code_int = next((k for k, v in STATUS_MAP.items() if v == status_raw), None)
    status_hex      = f"0x{status_code_int:04X}" if status_code_int is not None else "0xFFFF"
    status_desc     = STATUS_DESCRIPTION.get(status_code_int, status_raw) if status_code_int is not None else status_raw

    return {
        "brand":                  cfg["brand"],
        "serial":                 values.get("serial", ""),
        "inverter_rated_power_w": int(rated_w),
        "mppt_count":             mppt_count,
        "status":       status_raw,
        "status_hex":   status_hex,
        "status_desc":  status_desc,
        "status_code_int": status_code_int if status_code_int is not None else -1,
        "fault_data":   fault_data,
        "any_fault":    any_fault,
        "op_status_raw":  op_int,
        "op_status_hex":  f"0x{op_int:04X}",
        "op_status_bits": op_bits,
        "op_status_label": op_label,
        "inverter_temperature_c": f1s("inverter_temperature"),
        "battery_temperature_c":  f1s("battery_temperature"),
        "grid_frequency_hz":      f"{n('grid_frequency'):.2f}",
        "pv_strings":             pv_strings,
        "active_pv_strings":      active_pv,
        "pv_total_power_w":       f0s("pv_total_power"),
        "inverter_phases":        inverter_phases,
        "inverter_active_power_w": f0s("inverter_active_power"),
        "inverter_output_pct":    load_pct(inv_active, rated_w),
        "inverter_power_factor":     f"{inv_pf:.3f}" if inv_pf is not None else "N/A",
        "inverter_power_factor_num": inv_pf,
        "grid_phases":            grid_phases,
        "grid_power_w":           fmt0(total_grid),
        "grid_power_abs_w":       fmt0(abs(total_grid)),
        "grid_direction":         "Importing" if total_grid >= 0 else "Exporting",
        "grid_power_pct":         load_pct(total_grid, rated_w),
        "backup_phases":          backup_phases,
        "backup_load_power_w":    f0s("backup_load_power"),
        "backup_power_pct":       load_pct(backup_total, rated_w),
        "backup_power_factor":       f"{backup_pf:.3f}" if backup_pf is not None else "N/A",
        "backup_power_factor_num":   backup_pf,
        "battery_soc_pct":        f1s("battery_soc"),
        "battery_soh_pct":        f1s("battery_soh"),
        "battery_voltage_v":      f1s("battery_voltage"),
        "battery_voltage_bms_v":  f1s("battery_voltage_bms"),
        "battery_current_a":      f2s("battery_current_bms") if values.get("battery_current_bms") is not None else f2s("battery_current"),
        "battery_current_bms_a":  f2s("battery_current_bms"),
        "battery_power_w":        f0s("battery_power"),
        "battery_direction":      values.get("battery_direction", "Unknown"),
        "battery_charge_current_limit_bms_a":    f1s("battery_charge_current_limit_bms"),
        "battery_discharge_current_limit_bms_a": f1s("battery_discharge_current_limit_bms"),
        "battery_fault_status_1_bms":     values.get("battery_fault_status_1_bms", "n/a"),
        "battery_fault_status_2_bms":     values.get("battery_fault_status_2_bms", "n/a"),
        "battery_fault_status_1_bms_int": hex_to_int(values.get("battery_fault_status_1_bms")),
        "battery_fault_status_2_bms_int": hex_to_int(values.get("battery_fault_status_2_bms")),
        "ac_grid_port_power_w":   f0s("ac_grid_port_power"),
        "household_load_power_w": f0s("household_load_power"),
        "pv_generation_today_kwh":       f"{n('pv_today_energy_generation'):.2f}",
        "pv_generation_total_kwh":       f"{n('pv_total_energy_generation'):.1f}",
        "load_consumption_today_kwh":    f"{n('load_energy_today'):.2f}",
        "load_consumption_total_kwh":    f"{n('load_energy_total'):.1f}",
        "grid_import_today_kwh":         f"{n('grid_import_energy_today'):.2f}",
        "grid_import_total_kwh":         f"{n('grid_import_energy_total'):.1f}",
        "grid_export_today_kwh":         f"{n('grid_export_energy_today'):.2f}",
        "grid_export_total_kwh":         f"{n('grid_export_energy_total'):.1f}",
        "battery_charge_today_kwh":      f"{n('battery_charge_energy_today'):.2f}",
        "battery_charge_total_kwh":      f"{n('battery_charge_energy_total'):.1f}",
        "battery_discharge_today_kwh":   f"{n('battery_discharge_energy_today'):.2f}",
        "battery_discharge_total_kwh":   f"{n('battery_discharge_energy_total'):.1f}",
        "household_load_today_kwh":  f"{n('household_load_energy_today'):.2f}",
        "household_load_month_kwh":  f"{n('household_load_energy_month'):.1f}",
        "household_load_year_kwh":   f"{n('household_load_energy_year'):.1f}",
        "household_load_total_kwh":  f"{n('household_load_energy_total'):.1f}",
        "backup_load_today_kwh":     f"{n('backup_load_energy_today'):.2f}",
        "backup_load_month_kwh":     f"{n('backup_load_energy_month'):.1f}",
        "backup_load_year_kwh":      f"{n('backup_load_energy_year'):.1f}",
        "backup_load_total_kwh":     f"{n('backup_load_energy_total'):.1f}",
        "battery2": None,  # Solis second-battery registers not identified yet
    }

# ── Serial-mismatch error contexts ────────────────────────────────────────────

def serial_error_prometheus(expected, read, brand):
    return (
        f"# ERROR serial mismatch: expected={expected} read={read}\n"
        f'inverter_serial_mismatch{{brand="{brand}",'
        f'expected="{expected}",read="{read}"}} 1\n'
    )

def serial_error_human(expected, read):
    return (
        f"ERROR: Serial mismatch\n"
        f"  Expected: {expected}\n"
        f"  Read:     {read}\n"
        f"No data returned.\n"
    )

# ── Jinja2 rendering ──────────────────────────────────────────────────────────

def _jinja_jv(v):
    """Serialize a context value to a JSON literal (number, bool, null, or string)."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return "null"
        return str(int(v)) if v == int(v) else str(v)
    s = str(v)
    if s in ("NaN", "N/A"):
        return "null"
    try:
        f = float(s)
        if math.isnan(f) or math.isinf(f):
            return "null"
        return str(int(f)) if f == int(f) else s
    except (ValueError, TypeError):
        return json.dumps(s)

def get_jinja_env():
    if not TEMPLATES_DIR.exists():
        raise FileNotFoundError(
            f"Templates directory not found: {TEMPLATES_DIR}\n"
            "Please create ./templates/ with files: prometheus, human, "
            "prometheus-solis-specific, human-solis-specific"
        )
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["jv"] = _jinja_jv
    return env

def render(fmt, ctx, solis_specific=True):
    env    = get_jinja_env()
    output = env.get_template(fmt).render(**ctx)
    if solis_specific:
        try:
            specific = env.get_template(f"{fmt}-solis-specific").render(**ctx)
            output   = output.rstrip("\n") + "\n" + specific
        except Exception:
            pass
    print(output)

# ── CLI / main ────────────────────────────────────────────────────────────────

def dump_tou_registers(regmap):
    """Print raw TOU register values to stdout for debugging."""
    SLOT_LABELS = ["soc_cutoff", "current   ", "cutoff_v  ", "start_hour", "start_min ", "end_hour  ", "end_min   "]
    switch = regmap.get(TOU_BLOCK_START)
    sw_str = f"{switch}  (0x{switch:04X})" if switch is not None else "N/A"
    print("=== TOU Register Dump ===")
    print(f"43707  TOU V2 Switch : {sw_str}")
    if switch is not None:
        charge_bits    = " ".join(f"C{i+1}={'1' if (switch >> i)     & 1 else '0'}" for i in range(6))
        discharge_bits = " ".join(f"D{i+1}={'1' if (switch >> (6+i)) & 1 else '0'}" for i in range(6))
        print(f"         Bitmask    : {charge_bits}  {discharge_bits}")

    for label, base in [("Charge", TOU_CHARGE_START), ("Discharge", TOU_DISCHARGE_START)]:
        print(f"\n{label} Slots:")
        for slot in range(6):
            slot_base = base + slot * TOU_SLOT_FIELDS
            print(f"  Slot {slot + 1} (base {slot_base}):")
            for field, name in enumerate(SLOT_LABELS):
                reg = slot_base + field
                val = regmap.get(reg)
                val_str = f"{val:6d}  (0x{val:04X})" if val is not None else "   N/A"
                print(f"    {reg}  {name} : {val_str}")

def parse_args():
    parser = argparse.ArgumentParser(description="Solis Modbus poller")
    parser.add_argument("--format", choices=["human", "prometheus", "json"], default="human")
    parser.add_argument("--no-solis-specific", action="store_true",
                        help="Skip the solis-specific template section")
    parser.add_argument("--dump-tou", action="store_true",
                        help="Print raw TOU register values and exit (for debugging)")
    parser.add_argument("--dump-storage", action="store_true",
                        help="Print raw storage setting register values and exit (for debugging)")
    return parser.parse_args()

def _connect(ip, port, timeout=5, retries=3):
    """Create and connect a ModbusTcpClient, retrying once on failure."""
    client = ModbusTcpClient(ip, port=port, timeout=timeout, retries=retries)
    if client.connect():
        return client
    # First attempt failed – wait briefly and retry
    time.sleep(1.0)
    client.close()
    client = ModbusTcpClient(ip, port=port, timeout=timeout, retries=retries)
    if client.connect():
        return client
    raise SystemExit(f"Could not connect to {ip}:{port}")

def main():
    args = parse_args()
    cfg  = load_config()

    client = _connect(cfg["ip"], cfg["port"])
    try:
        if args.dump_tou:
            dump_tou_registers(read_tou_registers(client, cfg))
            return
        if args.dump_storage:
            regmap = {}
            offset = 1 if cfg.get("use_zero_based_addressing", "false").lower() == "true" else 0
            slave_id = int(cfg.get("slave_id", 1))
            SCAN_RANGES = [(40000, 41001), (43000, 45001)]
            for scan_start, scan_end in SCAN_RANGES:
                print(f"Scanning holding registers {scan_start}-{scan_end - 1}...", file=sys.stderr)
                for start in range(scan_start, scan_end, 100):
                    count = min(100, scan_end - start)
                    addr = start - offset
                    try:
                        rr = client.read_holding_registers(address=addr, count=count, device_id=slave_id)
                        if not rr.isError():
                            for i, v in enumerate(rr.registers):
                                regmap[start + i] = v
                            continue
                    except Exception:
                        pass
                    for sub in range(start, start + count, 10):
                        sub_count = min(10, start + count - sub)
                        try:
                            rr = client.read_holding_registers(address=sub - offset, count=sub_count, device_id=slave_id)
                            if not rr.isError():
                                for i, v in enumerate(rr.registers):
                                    regmap[sub + i] = v
                        except Exception:
                            pass
            dump_storage_registers(regmap)
            return

        values, skipped = None, []

        for attempt in range(MAX_POLL_ATTEMPTS):
            regmap         = read_registers(client, cfg)
            values, skipped = build_values(regmap, cfg)
            none_count     = sum(1 for v in values.values() if v is None)

            if none_count <= MAX_NONE_THRESHOLD or attempt == MAX_POLL_ATTEMPTS - 1:
                break

            print(
                f"#WARN: {none_count} None-valued sensors on poll attempt "
                f"{attempt + 1}/{MAX_POLL_ATTEMPTS}, retrying in {POLL_RETRY_DELAY}s…",
                file=sys.stderr,
            )
            time.sleep(POLL_RETRY_DELAY)

        # ── Serial check ──
        serial_ok, read_serial = check_serial(values, cfg)
        if not serial_ok:
            exp = cfg["expected_serial"]
            if args.format == "prometheus":
                print(serial_error_prometheus(exp, read_serial, cfg["brand"]))
            elif args.format == "json":
                print(json.dumps({"error": "serial_mismatch", "expected": exp, "read": read_serial}))
            else:
                print(serial_error_human(exp, read_serial))
            sys.exit(2)

        # Diagnostics to stderr
        for name, regs, reason in skipped:
            regtxt = str(regs[0]) if len(regs) == 1 else f"{regs[0]}-{regs[-1]}"
            print(f"#SKIPPED [{regtxt}] {name}: {reason}", file=sys.stderr)

        none_keys = [k for k, v in values.items() if v is None]
        if none_keys:
            print(f"#OUT-OF-RANGE keys (rendered as NaN): {', '.join(none_keys)}", file=sys.stderr)

        ctx = build_context(values, cfg)
        if args.format == "human" and skipped:
            ctx["_skipped"] = skipped

        tou_regmap = read_tou_registers(client, cfg)
        ctx["tou"] = decode_tou(tou_regmap)

        storage_regmap = read_storage_registers(client, cfg)
        ctx["storage"] = decode_storage(storage_regmap)

        render(args.format, ctx, solis_specific=not args.no_solis_specific)

    finally:
        client.close()

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
