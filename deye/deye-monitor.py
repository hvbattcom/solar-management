#!/usr/bin/env python3
"""
deye-monitor.py – SolarmanV5 poller for Deye inverters.
Outputs human-readable, Prometheus, or JSON metrics via Jinja2 templates.
Uses the shared templates/ directory at project root (same schema as solis-monitor).
"""

import argparse
import configparser
import json
import math
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

try:
    from pysolarmanv5 import PySolarmanV5
except ImportError:
    PySolarmanV5 = None

CONFIG_FILE = "config.cfg"
SECTION = "DeyeInverter"
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# ── Register map ──────────────────────────────────────────────────────────────
REGISTER_DEFINITIONS = {
    0x02A0: {'name': 'PV1 Power',           'scale': 10,  'unit': 'W',   'group': 'solar',    'signed': False},
    0x02A1: {'name': 'PV2 Power',           'scale': 10,  'unit': 'W',   'group': 'solar',    'signed': False},
    0x02A2: {'name': 'PV3 Power',           'scale': 10,  'unit': 'W',   'group': 'solar',    'signed': False},
    0x02A3: {'name': 'PV4 Power',           'scale': 10,  'unit': 'W',   'group': 'solar',    'signed': False},
    0x02A4: {'name': 'PV1 Voltage',         'scale': 0.1, 'unit': 'V',   'group': 'solar',    'signed': False},
    0x02A5: {'name': 'PV1 Current',         'scale': 0.1, 'unit': 'A',   'group': 'solar',    'signed': False},
    0x02A6: {'name': 'PV2 Voltage',         'scale': 0.1, 'unit': 'V',   'group': 'solar',    'signed': False},
    0x02A7: {'name': 'PV2 Current',         'scale': 0.1, 'unit': 'A',   'group': 'solar',    'signed': False},
    0x02A8: {'name': 'PV3 Voltage',         'scale': 0.1, 'unit': 'V',   'group': 'solar',    'signed': False},
    0x02A9: {'name': 'PV3 Current',         'scale': 0.1, 'unit': 'A',   'group': 'solar',    'signed': False},
    0x02AA: {'name': 'PV4 Voltage',         'scale': 0.1, 'unit': 'V',   'group': 'solar',    'signed': False},
    0x02AB: {'name': 'PV4 Current',         'scale': 0.1, 'unit': 'A',   'group': 'solar',    'signed': False},
    0x0211: {'name': 'Daily Production',    'scale': 0.1, 'unit': 'kWh', 'group': 'solar',    'signed': False},
    0x0216: {'name': 'Total Production Low','scale': 0.1, 'unit': 'kWh', 'group': 'solar',    'signed': False},
    0x0217: {'name': 'Total Production High','scale': 0.1,'unit': 'kWh', 'group': 'solar',    'signed': False},

    0x006C: {'name': 'Battery Max A Charge',    'scale': 1,   'unit': 'A',   'group': 'battery',  'signed': False},
    0x006D: {'name': 'Battery Max A Discharge', 'scale': 1,   'unit': 'A',   'group': 'battery',  'signed': False},
    0x0202: {'name': 'Daily Battery Charge',    'scale': 0.1, 'unit': 'kWh', 'group': 'battery',  'signed': False},
    0x0203: {'name': 'Daily Battery Discharge', 'scale': 0.1, 'unit': 'kWh', 'group': 'battery',  'signed': False},
    0x0204: {'name': 'Total Battery Charge Low',    'scale': 0.1, 'unit': 'kWh', 'group': 'battery', 'signed': False},
    0x0205: {'name': 'Total Battery Charge High',   'scale': 0.1, 'unit': 'kWh', 'group': 'battery', 'signed': False},
    0x0206: {'name': 'Total Battery Discharge Low', 'scale': 0.1, 'unit': 'kWh', 'group': 'battery', 'signed': False},
    0x0207: {'name': 'Total Battery Discharge High','scale': 0.1, 'unit': 'kWh', 'group': 'battery', 'signed': False},
    0x024A: {'name': 'Battery Temperature', 'scale': 0.1, 'unit': '°C', 'group': 'battery', 'signed': False, 'offset': 1000},
    0x024B: {'name': 'Battery Voltage',     'scale': 0.1, 'unit': 'V',  'group': 'battery', 'signed': False},
    0x024C: {'name': 'Battery SOC',         'scale': 1,   'unit': '%',  'group': 'battery', 'signed': False},
    0x024E: {'name': 'Battery Power',       'scale': 10,  'unit': 'W',  'group': 'battery', 'signed': True},
    0x024F: {'name': 'Battery Current',     'scale': 0.01,'unit': 'A',  'group': 'battery', 'signed': True},

    0x0256: {'name': 'Grid Voltage L1',       'scale': 0.1, 'unit': 'V', 'group': 'grid', 'signed': False},
    0x0257: {'name': 'Grid Voltage L2',       'scale': 0.1, 'unit': 'V', 'group': 'grid', 'signed': False},
    0x0258: {'name': 'Grid Voltage L3',       'scale': 0.1, 'unit': 'V', 'group': 'grid', 'signed': False},
    0x0268: {'name': 'External CT L1 Power', 'scale': 1,   'unit': 'W', 'group': 'grid', 'signed': True},
    0x0269: {'name': 'External CT L2 Power', 'scale': 1,   'unit': 'W', 'group': 'grid', 'signed': True},
    0x026A: {'name': 'External CT L3 Power', 'scale': 1,   'unit': 'W', 'group': 'grid', 'signed': True},
    0x0271: {'name': 'Total Grid Power',     'scale': 1,   'unit': 'W', 'group': 'grid', 'signed': True},
    0x0208: {'name': 'Daily Energy Bought',        'scale': 0.1, 'unit': 'kWh', 'group': 'grid', 'signed': False},
    0x020A: {'name': 'Total Energy Bought Low',    'scale': 0.1, 'unit': 'kWh', 'group': 'grid', 'signed': False},
    0x020B: {'name': 'Total Energy Bought High',   'scale': 0.1, 'unit': 'kWh', 'group': 'grid', 'signed': False},
    0x0209: {'name': 'Daily Energy Sold',          'scale': 0.1, 'unit': 'kWh', 'group': 'grid', 'signed': False},
    0x020C: {'name': 'Total Energy Sold Low',      'scale': 0.1, 'unit': 'kWh', 'group': 'grid', 'signed': False},
    0x020D: {'name': 'Total Energy Sold High',     'scale': 0.1, 'unit': 'kWh', 'group': 'grid', 'signed': False},

    # Deye "Load" = EPS/backup output (same physical role as Solis "Backup")
    0x028A: {'name': 'Load L1 Power',    'scale': 1,   'unit': 'W', 'group': 'load', 'signed': True},
    0x028B: {'name': 'Load L2 Power',    'scale': 1,   'unit': 'W', 'group': 'load', 'signed': True},
    0x028C: {'name': 'Load L3 Power',    'scale': 1,   'unit': 'W', 'group': 'load', 'signed': True},
    0x028D: {'name': 'Total Load Power', 'scale': 1,   'unit': 'W', 'group': 'load', 'signed': True},
    0x0284: {'name': 'Load Voltage L1',  'scale': 0.1, 'unit': 'V', 'group': 'load', 'signed': False},
    0x0285: {'name': 'Load Voltage L2',  'scale': 0.1, 'unit': 'V', 'group': 'load', 'signed': False},
    0x0286: {'name': 'Load Voltage L3',  'scale': 0.1, 'unit': 'V', 'group': 'load', 'signed': False},
    0x020E: {'name': 'Daily Load Consumption',        'scale': 0.1, 'unit': 'kWh', 'group': 'load', 'signed': False},
    0x020F: {'name': 'Total Load Consumption Low',    'scale': 0.1, 'unit': 'kWh', 'group': 'load', 'signed': False},
    0x0210: {'name': 'Total Load Consumption High',   'scale': 0.1, 'unit': 'kWh', 'group': 'load', 'signed': False},

    0x0276: {'name': 'Current L1',       'scale': 0.01, 'unit': 'A', 'group': 'inverter', 'signed': True},
    0x0277: {'name': 'Current L2',       'scale': 0.01, 'unit': 'A', 'group': 'inverter', 'signed': True},
    0x0278: {'name': 'Current L3',       'scale': 0.01, 'unit': 'A', 'group': 'inverter', 'signed': True},
    0x0279: {'name': 'Inverter L1 Power','scale': 1,    'unit': 'W', 'group': 'inverter', 'signed': True},
    0x027A: {'name': 'Inverter L2 Power','scale': 1,    'unit': 'W', 'group': 'inverter', 'signed': True},
    0x027B: {'name': 'Inverter L3 Power','scale': 1,    'unit': 'W', 'group': 'inverter', 'signed': True},
    0x021C: {'name': 'DC Temperature',   'scale': 0.1,  'unit': '°C','group': 'inverter', 'signed': True, 'offset': 1000},
    0x021D: {'name': 'AC Temperature',   'scale': 0.1,  'unit': '°C','group': 'inverter', 'signed': True, 'offset': 1000},

    0x0016: {'name': 'PV/Phase Config', 'scale': 1, 'unit': '', 'group': 'inverter', 'signed': False},

    # TOU / settings (holding registers)
    130:    {'name': 'Grid Charge Enable',    'scale': 1, 'unit': '', 'group': 'tou', 'signed': False},
    145:    {'name': 'Solar Sell Enable',     'scale': 1, 'unit': '', 'group': 'tou', 'signed': False},
    166:    {'name': 'ToU SOC Slot 1',        'scale': 1, 'unit': '%','group': 'tou', 'signed': False},
    167:    {'name': 'ToU SOC Slot 2',        'scale': 1, 'unit': '%','group': 'tou', 'signed': False},
    168:    {'name': 'ToU SOC Slot 3',        'scale': 1, 'unit': '%','group': 'tou', 'signed': False},
    169:    {'name': 'ToU SOC Slot 4',        'scale': 1, 'unit': '%','group': 'tou', 'signed': False},
    170:    {'name': 'ToU SOC Slot 5',        'scale': 1, 'unit': '%','group': 'tou', 'signed': False},
    171:    {'name': 'ToU SOC Slot 6',        'scale': 1, 'unit': '%','group': 'tou', 'signed': False},
    172:    {'name': 'ToU Charge Enable Slot 1', 'scale': 1, 'unit': '', 'group': 'tou', 'signed': False},
    173:    {'name': 'ToU Charge Enable Slot 2', 'scale': 1, 'unit': '', 'group': 'tou', 'signed': False},
    174:    {'name': 'ToU Charge Enable Slot 3', 'scale': 1, 'unit': '', 'group': 'tou', 'signed': False},
    175:    {'name': 'ToU Charge Enable Slot 4', 'scale': 1, 'unit': '', 'group': 'tou', 'signed': False},
    176:    {'name': 'ToU Charge Enable Slot 5', 'scale': 1, 'unit': '', 'group': 'tou', 'signed': False},
    177:    {'name': 'ToU Charge Enable Slot 6', 'scale': 1, 'unit': '', 'group': 'tou', 'signed': False},
    0x0094: {'name': 'ToU Time 1',  'scale': 1, 'unit': '', 'group': 'tou', 'signed': False},
    0x0095: {'name': 'ToU Time 2',  'scale': 1, 'unit': '', 'group': 'tou', 'signed': False},
    0x0096: {'name': 'ToU Time 3',  'scale': 1, 'unit': '', 'group': 'tou', 'signed': False},
    0x0097: {'name': 'ToU Time 4',  'scale': 1, 'unit': '', 'group': 'tou', 'signed': False},
    0x0098: {'name': 'ToU Time 5',  'scale': 1, 'unit': '', 'group': 'tou', 'signed': False},
    0x0099: {'name': 'ToU Time 6',  'scale': 1, 'unit': '', 'group': 'tou', 'signed': False},
    0x009A: {'name': 'ToU Power 1', 'scale': 1, 'unit': 'W','group': 'tou', 'signed': False},
    0x009B: {'name': 'ToU Power 2', 'scale': 1, 'unit': 'W','group': 'tou', 'signed': False},
    0x009C: {'name': 'ToU Power 3', 'scale': 1, 'unit': 'W','group': 'tou', 'signed': False},
    0x009D: {'name': 'ToU Power 4', 'scale': 1, 'unit': 'W','group': 'tou', 'signed': False},
    0x009E: {'name': 'ToU Power 5', 'scale': 1, 'unit': 'W','group': 'tou', 'signed': False},
    0x009F: {'name': 'ToU Power 6', 'scale': 1, 'unit': 'W','group': 'tou', 'signed': False},
}

# ── Register decode helpers ───────────────────────────────────────────────────

def _minutes_to_time(val: int) -> str:
    if val == 0 or val >= 2400:
        return "00:00"
    return f"{val // 100:02d}:{val % 100:02d}"

def _scaled_value(reg: int, raw) -> float:
    if raw is None:
        return 0.0
    meta = REGISTER_DEFINITIONS.get(reg, {})
    value = int(raw)
    if meta.get('signed') and value > 32767:
        value -= 65536
    if meta.get('offset', 0):
        value = abs(value - meta['offset'])
    return float(value) * float(meta.get('scale', 1))

# ── SN probe (discovery) ──────────────────────────────────────────────────────
# SolarmanV5 requires the logger SN in every request, but the device always
# echoes its real SN in bytes 7-10 of any response.  Sending SN=0 lets us
# read the SN without knowing it in advance.

def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else crc >> 1
    return crc

def probe_sn(ip: str, port: int = 8899, timeout: float = 5.0) -> int | None:
    """Connect to a Deye datalogger and return its logger SN without knowing it."""
    import socket, struct
    mb = struct.pack(">BBHH", 1, 3, 33004, 1)
    mb += struct.pack("<H", _crc16(mb))
    payload_len = 15 + len(mb)
    frame = bytearray()
    frame += b"\xA5"
    frame += struct.pack("<H", payload_len)
    frame += struct.pack("<H", 0x4510)      # control: data transfer request
    frame += b"\x01\x00"                    # sequence
    frame += b"\x00\x00\x00\x00"           # logger SN = 0 (probe)
    frame += b"\x02\x00\x00"               # frame type + sensor type
    frame += b"\x00\x00\x00\x00" * 3      # deliver/power-on/offset times
    frame += mb
    frame += b"\x00\x15"                   # checksum placeholder + end
    frame[-2] = sum(frame[1:-2]) & 0xFF
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.sendall(bytes(frame))
            sock.settimeout(timeout)
            data = sock.recv(256)
        if len(data) >= 11 and data[0] == 0xA5:
            (sn,) = struct.unpack("<I", data[7:11])
            return sn if sn else None
    except Exception:
        pass
    return None


# ── Data reading ──────────────────────────────────────────────────────────────

def _bulk_read(ip: str, sn: int, port: int = 8899, verbose: bool = False) -> dict:
    if PySolarmanV5 is None:
        raise RuntimeError("pysolarmanv5 not installed — run: pip install pysolarmanv5")
    ranges = [
        {'start': 0x0016, 'count': 1},    # PV/phase config
        {'start': 0x0094, 'count': 84},   # TOU + battery config
        {'start': 0x0202, 'count': 32},   # energy stats
        {'start': 0x024A, 'count': 68},   # live status
        {'start': 0x02A0, 'count': 12},   # PV strings
    ]
    inv = PySolarmanV5(ip, sn, port=port)
    data = {}
    for r in ranges:
        if verbose:
            print(f"Reading 0x{r['start']:04X}..+{r['count']}", file=sys.stderr)
        arr = inv.read_holding_registers(r['start'], r['count'])
        for i, v in enumerate(arr):
            data[r['start'] + i] = v
    inv.disconnect()
    return data

def _calc_32bit(low, high) -> float:
    low  = int(low  or 0) & 0xFFFF
    high = int(high or 0) & 0xFFFF
    return ((high << 16) | low) * 0.1

# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: Path = None) -> dict:
    cfg_path = Path(path) if path else Path(__file__).resolve().parent / CONFIG_FILE

    cfg = configparser.ConfigParser()
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    cfg.read(cfg_path, encoding="utf-8")
    if SECTION not in cfg:
        raise KeyError(f"Missing section [{SECTION}] in {cfg_path}")

    sec = cfg[SECTION]
    ip  = sec.get("inverter_ip", "").strip()
    if not ip:
        raise ValueError(f"Missing inverter_ip in [{SECTION}]")
    sn = sec.getint("inverter_sn", fallback=0)
    if not sn:
        raise ValueError(f"Missing inverter_sn in [{SECTION}]")

    return {
        "ip":             ip,
        "port":           sec.getint("inverter_port", fallback=8899),
        "sn":             sn,
        "brand":          "deye",
        "verbose":        sec.getboolean("verbose", fallback=False),
        "inverter_power_w": sec.getfloat("inverter_power_kw", fallback=8.0) * 1000.0,
    }

# ── Numeric helpers ───────────────────────────────────────────────────────────

def num(v, default=0.0):
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    return default

def fmt1(v): return "NaN" if v is None else f"{float(v):.1f}"
def fmt2(v): return "NaN" if v is None else f"{float(v):.2f}"
def fmt0(v): return "NaN" if v is None else f"{round(float(v)):.0f}"

def load_pct(watts, rated_w):
    if watts is None or rated_w <= 0:
        return None
    return round(min(100.0, max(0.0, abs(float(watts)) / rated_w * 100.0)), 1)

def _safe_div(a, b):
    return (a / b) if b else 0.0

# ── Section builders ──────────────────────────────────────────────────────────

def _pv_rows(get, mppt_count: int):
    spec = [
        ('PV1', 0x02A4, 0x02A5, 0x02A0),
        ('PV2', 0x02A6, 0x02A7, 0x02A1),
        ('PV3', 0x02A8, 0x02A9, 0x02A2),
        ('PV4', 0x02AA, 0x02AB, 0x02A3),
    ][:mppt_count]
    rows, active, total = [], 0, 0.0
    for name, v_r, c_r, p_r in spec:
        v, c, p = get(v_r), get(c_r), get(p_r)
        rows.append({'name': name, 'voltage': v, 'current': c, 'power': p})
        if p > 0:
            active += 1
            total  += p
    return rows, active, total

def _tou_rows(get) -> list:
    slot_defs = [
        {'time': 0x0094, 'next': 0x0095, 'pwr': 0x009A, 'soc': 166, 'ctrl': 172},
        {'time': 0x0095, 'next': 0x0096, 'pwr': 0x009B, 'soc': 167, 'ctrl': 173},
        {'time': 0x0096, 'next': 0x0097, 'pwr': 0x009C, 'soc': 168, 'ctrl': 174},
        {'time': 0x0097, 'next': 0x0098, 'pwr': 0x009D, 'soc': 169, 'ctrl': 175},
        {'time': 0x0098, 'next': 0x0099, 'pwr': 0x009E, 'soc': 170, 'ctrl': 176},
        {'time': 0x0099, 'next': 0x0094, 'pwr': 0x009F, 'soc': 171, 'ctrl': 177},
    ]
    rows = []
    for sl in slot_defs:
        ctrl = int(get(sl['ctrl']))
        rows.append({
            'grid':      '✓' if (ctrl & 1)  else ' ',
            'gen':       '✓' if (ctrl & 2)  else ' ',
            'sell':      '✓' if (ctrl & 32) else ' ',
            'grid_flag': 1   if (ctrl & 1)  else 0,
            'gen_flag':  1   if (ctrl & 2)  else 0,
            'sell_flag': 1   if (ctrl & 32) else 0,
            'start': _minutes_to_time(int(get(sl['time']))),
            'end':   _minutes_to_time(int(get(sl['next']))),
            'power': int(get(sl['pwr'])) * 10,
            'soc':   int(get(sl['soc'])),
        })
    return rows

def _bat_direction(power: float) -> str:
    if power > 1:   return "Discharging"
    if power < -1:  return "Charging"
    return "Idle"

# ── Context builder ───────────────────────────────────────────────────────────

def build_context(raw: dict, cfg: dict) -> dict:
    def get(reg: int) -> float:
        return _scaled_value(reg, raw.get(reg))

    rated_w    = cfg["inverter_power_w"]
    raw_cfg    = raw.get(0x0016, 0)
    mppt_from_reg = raw_cfg >> 8   # high byte = MPPT count
    mppt_count = mppt_from_reg if 1 <= mppt_from_reg <= 4 else 4
    per_phase_w = rated_w / 3.0

    # ── Status (derived – no dedicated register on Deye) ─────────────────────
    soc        = get(0x024C)
    pv_total_w = sum(get(r) for r in (0x02A0, 0x02A1, 0x02A2, 0x02A3))
    if pv_total_w > 10:  status = "GridConnected"
    elif soc > 20:       status = "Battery"
    else:                status = "Standby"

    # ── PV strings ────────────────────────────────────────────────────────────
    pv_raw, active_pv, pv_total_power = _pv_rows(get, mppt_count)
    pv_strings = [
        {"name": r["name"], "voltage": fmt1(r["voltage"]),
         "current": fmt2(r["current"]), "power": fmt0(r["power"])}
        for r in pv_raw
    ]

    # ── Inverter output phases ────────────────────────────────────────────────
    inv_regs = [(0x0256, 0x0276, 0x0279),
                (0x0257, 0x0277, 0x027A),
                (0x0258, 0x0278, 0x027B)]
    inverter_phases = []
    inverter_total  = 0.0
    for i, (v_r, c_r, p_r) in enumerate(inv_regs):
        v, c, p = get(v_r), get(c_r), get(p_r)
        inverter_total += p
        inverter_phases.append({
            "name":     f"L{i + 1}",
            "voltage":  fmt1(v),
            "current":  fmt2(c),
            "power":    fmt0(p),
            "load_pct": load_pct(p, per_phase_w),
        })

    # ── Grid phases (external CT) ─────────────────────────────────────────────
    grid_regs = [(0x0256, 0x0268),
                 (0x0257, 0x0269),
                 (0x0258, 0x026A)]
    grid_phases = []
    for i, (v_r, p_r) in enumerate(grid_regs):
        v, p = get(v_r), get(p_r)
        grid_phases.append({
            "name":             f"L{i + 1}",
            "voltage":          fmt1(v),
            "current":          fmt2(_safe_div(p, v) if v else 0.0),
            "power":            fmt0(p),
            "power_factor":     "N/A",
            "power_factor_num": None,
            "load_pct":         load_pct(p, per_phase_w),
        })
    grid_total = get(0x0271)

    # ── Backup/EPS phases (Deye "Load" = EPS output = Solis "Backup") ────────
    load_regs = [(0x0284, 0x028A),
                 (0x0285, 0x028B),
                 (0x0286, 0x028C)]
    backup_phases = []
    for i, (v_r, p_r) in enumerate(load_regs):
        v, p = get(v_r), get(p_r)
        backup_phases.append({
            "name":     f"L{i + 1}",
            "voltage":  fmt1(v),
            "current":  fmt2(_safe_div(p, v) if v else 0.0),
            "power":    fmt0(p),
            "load_pct": load_pct(p, per_phase_w),
        })
    backup_total = get(0x028D)

    # ── Battery ───────────────────────────────────────────────────────────────
    bat_pwr = get(0x024E)
    bat_dir = _bat_direction(bat_pwr)

    # ── Energy counters ───────────────────────────────────────────────────────
    daily_pv     = get(0x0211)
    daily_load   = get(0x020E)
    daily_import = get(0x0208)
    daily_export = get(0x0209)
    daily_chg    = get(0x0202)
    daily_dis    = get(0x0203)

    total_pv     = _calc_32bit(raw.get(0x0216), raw.get(0x0217))
    total_load   = _calc_32bit(raw.get(0x020F), raw.get(0x0210))
    total_import = _calc_32bit(raw.get(0x020A), raw.get(0x020B))
    total_export = _calc_32bit(raw.get(0x020C), raw.get(0x020D))
    total_chg    = _calc_32bit(raw.get(0x0204), raw.get(0x0205))
    total_dis    = _calc_32bit(raw.get(0x0206), raw.get(0x0207))

    # ── TOU ───────────────────────────────────────────────────────────────────
    grid_chg_en   = int(get(130))  != 0
    solar_sell_en = int(get(145))  != 0
    tou_rows      = _tou_rows(get)

    return {
        # ── Universal identity ─────────────────────────────────────────────────
        "brand":                "deye",
        "serial":               str(cfg["sn"]),
        "inverter_rated_power_w": int(rated_w),
        "mppt_count":           mppt_count,

        # ── Status ────────────────────────────────────────────────────────────
        "status":          status,
        "status_hex":      "0xFFFF",
        "status_desc":     status,
        "status_code_int": -1,

        # ── Temperatures / frequency ──────────────────────────────────────────
        "inverter_temperature_c": fmt1(get(0x021D)),  # AC side temperature
        "battery_temperature_c":  fmt1(get(0x024A)),
        "grid_frequency_hz":      "0.00",             # not available on Deye

        # ── PV ────────────────────────────────────────────────────────────────
        "pv_strings":        pv_strings,
        "active_pv_strings": active_pv,
        "pv_total_power_w":  fmt0(pv_total_power),

        # ── Inverter output ───────────────────────────────────────────────────
        "inverter_phases":            inverter_phases,
        "inverter_active_power_w":    fmt0(inverter_total),
        "inverter_output_pct":        load_pct(inverter_total, rated_w),
        "inverter_power_factor":      "N/A",
        "inverter_power_factor_num":  None,

        # ── Grid exchange ─────────────────────────────────────────────────────
        "grid_phases":       grid_phases,
        "grid_power_w":      fmt0(grid_total),
        "grid_power_abs_w":  fmt0(abs(grid_total)),
        "grid_direction":    "Importing" if grid_total >= 0 else "Exporting",
        "grid_power_pct":    load_pct(grid_total, rated_w),

        # ── Backup/EPS (= Deye Load output) ───────────────────────────────────
        "backup_phases":         backup_phases,
        "backup_load_power_w":   fmt0(backup_total),
        "backup_power_pct":      load_pct(backup_total, rated_w),
        "backup_power_factor":      "N/A",
        "backup_power_factor_num":  None,

        # ── Battery ───────────────────────────────────────────────────────────
        "battery_voltage_v":   fmt1(get(0x024B)),
        "battery_current_a":   fmt2(get(0x024F)),
        "battery_power_w":     fmt0(bat_pwr),
        "battery_direction":   bat_dir,
        "battery_soc_pct":     fmt1(soc),
        "battery_soh_pct":     "0.0",   # not available on Deye
        # BMS keys – null for Deye (required by universal json template)
        "battery_voltage_bms_v":                   None,
        "battery_current_bms_a":                   None,
        "battery_charge_current_limit_bms_a":      None,
        "battery_discharge_current_limit_bms_a":   None,

        # ── Battery 2 (placeholder – registers not identified yet) ───────────
        "battery2": None,

        # ── Energy counters ───────────────────────────────────────────────────
        "pv_generation_today_kwh":       f"{daily_pv:.2f}",
        "pv_generation_total_kwh":       f"{total_pv:.1f}",
        "load_consumption_today_kwh":    f"{daily_load:.2f}",
        "load_consumption_total_kwh":    f"{total_load:.1f}",
        "grid_import_today_kwh":         f"{daily_import:.2f}",
        "grid_import_total_kwh":         f"{total_import:.1f}",
        "grid_export_today_kwh":         f"{daily_export:.2f}",
        "grid_export_total_kwh":         f"{total_export:.1f}",
        "battery_charge_today_kwh":      f"{daily_chg:.2f}",
        "battery_charge_total_kwh":      f"{total_chg:.1f}",
        "battery_discharge_today_kwh":   f"{daily_dis:.2f}",
        "battery_discharge_total_kwh":   f"{total_dis:.1f}",

        # Solis sub-meter keys – null for Deye (required by universal json template)
        "battery_fault_status_1_bms":     None,
        "battery_fault_status_2_bms":     None,
        "battery_fault_status_1_bms_int": 0,
        "battery_fault_status_2_bms_int": 0,
        "ac_grid_port_power_w":           None,
        "household_load_power_w":         None,
        "household_load_today_kwh":       None,
        "household_load_month_kwh":       None,
        "household_load_year_kwh":        None,
        "household_load_total_kwh":       None,
        "backup_load_today_kwh":          f"{daily_load:.2f}",
        "backup_load_month_kwh":          None,
        "backup_load_year_kwh":           None,
        "backup_load_total_kwh":          f"{total_load:.1f}",

        # ── Fault & status stubs (no dedicated registers on Deye) ─────────────
        "fault_data":      {},
        "any_fault":       False,
        "op_status_raw":   0,
        "op_status_hex":   "0x0000",
        "op_status_bits":  [],
        "op_status_label": "OK",

        # ── Settings ──────────────────────────────────────────────────────────
        "storage": None,  # Solis-specific concept
        "tou":     None,  # Deye TOU shown in deye-specific template instead

        # ── Deye-specific extras (for deye-specific templates) ────────────────
        "dc_temperature_c":      fmt1(get(0x021C)),
        "ac_temperature_c":      fmt1(get(0x021D)),
        "temperatures": [
            {"sensor": "DC",   "value": fmt1(get(0x021C))},
            {"sensor": "AC",   "value": fmt1(get(0x021D))},
            {"sensor": "BAT1", "value": fmt1(get(0x024A))},
        ],
        "grid_charge_enable":    "Yes" if grid_chg_en   else "No",
        "solar_sell_enable":     "Yes" if solar_sell_en else "No",
        "grid_charge_enable_num": 1 if grid_chg_en   else 0,
        "solar_sell_enable_num":  1 if solar_sell_en else 0,
        "tou_rows":              tou_rows,
    }

# ── Storage / register dump ──────────────────────────────────────────────────

# Default scan range: covers battery config, TOU, live status, PV (all known registers)
DUMP_DEFAULT_START = 0x0060
DUMP_DEFAULT_END   = 0x02B0

def _scan_holding(ip: str, sn: int, port: int, start: int, end: int, verbose: bool) -> dict:
    """Read all holding registers in [start, end] in chunks, gracefully skipping failures."""
    if PySolarmanV5 is None:
        raise RuntimeError("pysolarmanv5 not installed — run: pip install pysolarmanv5")
    inv    = PySolarmanV5(ip, sn, port=port)
    regmap = {}
    addr   = start
    while addr <= end:
        chunk = min(100, end - addr + 1)
        if verbose:
            print(f"  Scanning 0x{addr:04X}..+{chunk}", file=sys.stderr)
        try:
            regs = inv.read_holding_registers(addr, chunk)
            for i, v in enumerate(regs):
                regmap[addr + i] = v
            addr += chunk
            continue
        except Exception:
            pass
        # Chunk failed – fall back to sub-chunks of 10
        for sub in range(addr, addr + chunk, 10):
            sub_count = min(10, addr + chunk - sub)
            try:
                regs = inv.read_holding_registers(sub, sub_count)
                for i, v in enumerate(regs):
                    regmap[sub + i] = v
            except Exception:
                pass
        addr += chunk
    inv.disconnect()
    return regmap

def dump_storage(ip: str, sn: int, port: int, start: int, end: int, verbose: bool):
    print(f"Scanning holding registers 0x{start:04X}–0x{end:04X} ({start}–{end})…",
          file=sys.stderr)
    regmap = _scan_holding(ip, sn, port, start, end, verbose)

    print(f"\n=== Deye Holding Register Dump (0x{start:04X}–0x{end:04X}) ===")
    print(f"{'Addr':>10}  {'Dec':>6}  {'Hex':>6}  {'Raw':>6}  Name / scaled value")
    print("-" * 72)

    for reg in sorted(regmap):
        val  = regmap[reg]
        meta = REGISTER_DEFINITIONS.get(reg)
        if meta:
            scale   = meta.get('scale', 1)
            unit    = meta.get('unit', '')
            signed  = meta.get('signed', False)
            offset  = meta.get('offset', 0)
            v       = int(val)
            if signed and v > 32767:
                v -= 65536
            if offset:
                v = abs(v - offset)
            scaled = v * scale
            if reg == 0x0016:
                label = f"PV/Phase Config  →  mppt={val >> 8}  phases={val & 0xFF}"
            else:
                label = f"{meta['name']}  →  {scaled:.4g} {unit}".rstrip()
            print(f"  0x{reg:04X}  {reg:6d}  {val:6d}  0x{val:04X}  {label}")
        elif val != 0:
            # Unknown register – only print if non-zero
            print(f"  0x{reg:04X}  {reg:6d}  {val:6d}  0x{val:04X}")

    print("-" * 72)
    print(f"  {len(regmap)} registers read, "
          f"{sum(1 for v in regmap.values() if v != 0)} non-zero")


# ── Jinja2 rendering (mirrors solis-monitor pattern) ─────────────────────────

def _jinja_jv(v):
    """Serialize a context value to a JSON literal."""
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
            "Run deye-monitor from inside the solar-management repo "
            "so that ../templates/ resolves correctly."
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

def render(fmt: str, ctx: dict, deye_specific: bool = True):
    print(render_to_str(fmt, ctx, deye_specific=deye_specific))

def render_to_str(fmt: str, ctx: dict, deye_specific: bool = True) -> str:
    env    = get_jinja_env()
    output = env.get_template(fmt).render(**ctx)
    if deye_specific:
        try:
            specific = env.get_template(f"{fmt}-deye-specific").render(**ctx)
            output   = output.rstrip("\n") + "\n" + specific
        except Exception:
            pass
    return output

def poll(cfg: dict) -> dict:
    raw = _bulk_read(cfg["ip"], cfg["sn"], port=cfg["port"], verbose=cfg.get("verbose", False))
    return build_context(raw, cfg)

# ── CLI / main ────────────────────────────────────────────────────────────────

def _parse_int(s: str) -> int:
    """Accept decimal or 0x… hex strings."""
    return int(s, 16) if s.lower().startswith("0x") else int(s)

def parse_args():
    parser = argparse.ArgumentParser(description="Deye SolarmanV5 poller")
    parser.add_argument("--format", choices=["human", "prometheus", "json"], default="human")
    parser.add_argument("--no-deye-specific", action="store_true",
                        help="Skip the deye-specific template section")
    parser.add_argument(
        "--dump-storage", nargs="*", metavar=("START", "END"),
        help=(
            "Scan holding registers and print all values with known names. "
            f"Optional range (decimal or 0x…): default 0x{DUMP_DEFAULT_START:04X}–0x{DUMP_DEFAULT_END:04X}. "
            "Examples: --dump-storage   --dump-storage 0x60 0xFF   --dump-storage 100 200"
        ),
    )
    parser.add_argument("--ip", metavar="IP",
                        help="Override inverter_ip from config (useful for discovery)")
    parser.add_argument("--port", type=int, metavar="PORT",
                        help="Override inverter_port from config")
    parser.add_argument("--sn", type=int, metavar="SN",
                        help="Override inverter_sn (datalogger serial) from config")
    parser.add_argument("--show-serial", action="store_true",
                        help="Connect and print the datalogger serial number then exit")
    return parser.parse_args()

def main():
    args = parse_args()

    cfg_path = Path(__file__).resolve().parent / CONFIG_FILE
    if args.ip and not cfg_path.exists():
        cfg = {
            "ip":               args.ip,
            "port":             args.port or 8899,
            "sn":               args.sn or 0,
            "brand":            "deye",
            "verbose":          False,
            "inverter_power_w": 30000.0,
        }
    else:
        cfg = load_config()

    if args.ip:
        cfg["ip"] = args.ip
    if args.port:
        cfg["port"] = args.port
    if args.sn:
        cfg["sn"] = args.sn

    if args.show_serial:
        if not cfg.get("sn"):
            sn = probe_sn(cfg["ip"], cfg["port"])
            print(sn if sn else "unknown")
        else:
            inv = PySolarmanV5(cfg["ip"], cfg["sn"], port=cfg["port"])
            inv.read_holding_registers(0x02A0, 1)  # minimal read to confirm connection
            inv.disconnect()
            print(cfg["sn"])
        return

    if args.dump_storage is not None:
        range_args = args.dump_storage
        if len(range_args) == 0:
            start, end = DUMP_DEFAULT_START, DUMP_DEFAULT_END
        elif len(range_args) == 2:
            start, end = _parse_int(range_args[0]), _parse_int(range_args[1])
        else:
            print("ERROR: --dump-storage takes 0 or 2 arguments (START END)", file=sys.stderr)
            sys.exit(1)
        dump_storage(cfg["ip"], cfg["sn"], cfg["port"], start, end, cfg["verbose"])
        return

    raw = _bulk_read(cfg["ip"], cfg["sn"], port=cfg["port"], verbose=cfg["verbose"])
    ctx = build_context(raw, cfg)

    render(args.format, ctx, deye_specific=not args.no_deye_specific)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
