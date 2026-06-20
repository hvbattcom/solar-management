#!/usr/bin/env python3
"""
deye-api.py — Flask HTTP API for Deye inverter settings via SolarmanV5.

Reads:
  GET /api/settings  — TOU + battery + general holding registers
  GET /api/status    — full poll via deye-monitor.py --format json

Writes (partial — only fields present in the body are changed):
  POST /api/settings/tou/<1-6>    — update a single TOU slot
  POST /api/settings/battery      — battery max charge/discharge current
  POST /api/settings/general      — grid charge enable, solar sell enable
"""

import argparse
import configparser
import subprocess
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

try:
    from pysolarmanv5 import PySolarmanV5
except ImportError:
    PySolarmanV5 = None

# ── Register map (decimal addresses) ─────────────────────────────────────────

REG_BAT_CHG_MAX     = 108   # 0x006C  Battery 1 Max Charge current (A)
REG_BAT_DIS_MAX     = 109   # 0x006D  Battery 1 Max Discharge current (A)
REG_GRID_CHARGE_EN  = 130   # 0=off 1=on
REG_ENERGY_MODE     = 140   # bit0-1: 0=Self Use, 2=Battery First, 3=Load First
REG_MAX_SELL_POWER  = 143   # raw × 10 = W
REG_SOLAR_SELL_EN   = 145   # 0=off 1=on
REG_TOU_TIME_BASE   = 148   # 148-153: slot 1-6 start time, HHMM integer
REG_TOU_POWER_BASE  = 154   # 154-159: slot 1-6 power, raw × 10 = W
REG_TOU_VOLTAGE_BASE= 160   # 160-165: slot 1-6 voltage (not exposed)
REG_TOU_SOC_BASE    = 166   # 166-171: slot 1-6 SOC %
REG_TOU_CTRL_BASE   = 172   # 172-177: slot 1-6 ctrl bitmask
REG_CTRL_SPECIAL_1  = 178   # bit4-5: 10=Grid PS disable, 11=Grid PS enable
REG_GRID_PS_POWER   = 191   # raw × 10 = W  (178+13)
REG_BAT2_CHG_MAX    = 243   # Battery 2 Max Charge current (A)
REG_BAT2_DIS_MAX    = 244   # Battery 2 Max Discharge current (A)

BIT_GRID_CHG = 0   # ctrl bit 0 = grid charging enable
BIT_GEN_CHG  = 1   # ctrl bit 1 = gen charging enable
BIT_SELL     = 5   # ctrl bit 5 = solar sell enable (per-slot)

ENERGY_MODES = {"Self Use": 0, "Battery First": 2, "Load First": 3}
ENERGY_MODES_BY_CODE = {v: k for k, v in ENERGY_MODES.items()}

# ── Config ────────────────────────────────────────────────────────────────────

_DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "deye-monitor" / "config.cfg"


def load_config(path: Path) -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(str(path))
    if "DeyeInverter" not in cfg:
        raise KeyError(f"[DeyeInverter] section not found in {path}")
    sec = cfg["DeyeInverter"]
    srv = cfg["DeyeAPI"] if "DeyeAPI" in cfg else {}
    return {
        "ip":             sec["inverter_ip"].strip(),
        "port":           int(sec.get("inverter_port", 8899)),
        "sn":             int(sec.get("inverter_sn", 0)),
        "monitor_script": str(Path(__file__).resolve().parent.parent / "deye-monitor" / "deye-monitor.py"),
        "server_host":    srv.get("host", "0.0.0.0").strip(),
        "server_port":    int(srv.get("port", 5001)),
    }


# ── SolarmanV5 helpers ────────────────────────────────────────────────────────

def _connect(cfg: dict) -> "PySolarmanV5":
    if PySolarmanV5 is None:
        raise RuntimeError("pysolarmanv5 not installed — run: pip install pysolarmanv5")
    return PySolarmanV5(cfg["ip"], cfg["sn"], port=cfg["port"])


def _read_regs(inv, start: int, count: int) -> list:
    return inv.read_holding_registers(start, count)


def _write_reg(inv, addr: int, value: int) -> None:
    inv.write_multiple_holding_registers(addr, [int(value)])


# ── Value encoding helpers ────────────────────────────────────────────────────

def _hhmm_to_str(val: int) -> str:
    return f"{val // 100:02d}:{val % 100:02d}"


def _str_to_hhmm(s: str) -> int:
    parts = str(s).split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time '{s}', expected HH:MM")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Time '{s}' out of range")
    return h * 100 + m


# ── High-level read ───────────────────────────────────────────────────────────

def read_settings(cfg: dict) -> dict:
    inv = _connect(cfg)
    try:
        bat1     = _read_regs(inv, REG_BAT_CHG_MAX, 2)        # 108-109
        gcen     = _read_regs(inv, REG_GRID_CHARGE_EN, 1)[0]   # 130
        blk140   = _read_regs(inv, REG_ENERGY_MODE, 4)         # 140-143
        ssen     = _read_regs(inv, REG_SOLAR_SELL_EN, 1)[0]    # 145
        tou_blk  = _read_regs(inv, REG_TOU_TIME_BASE, 30)      # 148-177
        blk178   = _read_regs(inv, REG_CTRL_SPECIAL_1, 14)     # 178-191
        bat2     = _read_regs(inv, REG_BAT2_CHG_MAX, 2)        # 243-244
    finally:
        inv.disconnect()

    mode_code    = blk140[0] & 3
    max_sell_raw = blk140[3]
    ctrl178      = blk178[0]
    grid_ps_raw  = blk178[13]                          # reg 191 = 178+13
    grid_ps_on   = ((ctrl178 >> 4) & 3) == 3           # bits 4-5 = 11 → enabled

    slots = []
    for i in range(6):
        time_raw  = tou_blk[i]
        power_raw = tou_blk[6 + i]
        soc_raw   = tou_blk[18 + i]
        ctrl_raw  = tou_blk[24 + i]
        slots.append({
            "slot":        i + 1,
            "time":        _hhmm_to_str(time_raw),
            "power_w":     power_raw * 10,
            "soc_pct":     soc_raw,
            "grid_charge": bool(ctrl_raw & (1 << BIT_GRID_CHG)),
            "gen_charge":  bool(ctrl_raw & (1 << BIT_GEN_CHG)),
            "sell":        bool(ctrl_raw & (1 << BIT_SELL)),
        })

    return {
        "general": {
            "energy_mode":               ENERGY_MODES_BY_CODE.get(mode_code, "Self Use"),
            "grid_charge_enable":        bool(gcen),
            "solar_sell_enable":         bool(ssen),
            "max_sell_power_w":          max_sell_raw * 10,
            "grid_peak_shaving_on":      grid_ps_on,
            "grid_peak_shaving_power_w": grid_ps_raw * 10,
        },
        "battery": {
            "max_charge_a":       bat1[0],
            "max_discharge_a":    bat1[1],
            "bat2_max_charge_a":    bat2[0],
            "bat2_max_discharge_a": bat2[1],
        },
        "tou": {"slots": slots},
    }


# ── High-level writes ─────────────────────────────────────────────────────────

def write_tou_slot(cfg: dict, slot_num: int, fields: dict) -> None:
    if not 1 <= slot_num <= 6:
        raise ValueError("slot must be 1-6")
    i   = slot_num - 1
    inv = _connect(cfg)
    try:
        # Read current slot values so partial updates work
        blk      = _read_regs(inv, REG_TOU_TIME_BASE, 30)
        time_raw  = blk[i]
        power_raw = blk[6 + i]
        soc_raw   = blk[18 + i]
        ctrl_raw  = blk[24 + i]

        if "time" in fields:
            time_raw = _str_to_hhmm(fields["time"])
        if "power_w" in fields:
            power_raw = int(int(fields["power_w"]) // 10)
        if "soc_pct" in fields:
            v = int(fields["soc_pct"])
            if not 0 <= v <= 100:
                raise ValueError("soc_pct must be 0-100")
            soc_raw = v

        for bit, key in ((BIT_GRID_CHG, "grid_charge"), (BIT_GEN_CHG, "gen_charge"), (BIT_SELL, "sell")):
            if key in fields:
                if fields[key]:
                    ctrl_raw |= 1 << bit
                else:
                    ctrl_raw &= ~(1 << bit)
        ctrl_raw &= 0xFFFF

        _write_reg(inv, REG_TOU_TIME_BASE  + i, time_raw)
        _write_reg(inv, REG_TOU_POWER_BASE + i, power_raw)
        _write_reg(inv, REG_TOU_SOC_BASE   + i, soc_raw)
        _write_reg(inv, REG_TOU_CTRL_BASE  + i, ctrl_raw)
    finally:
        inv.disconnect()


def write_battery(cfg: dict, fields: dict) -> None:
    inv = _connect(cfg)
    try:
        for key, reg, label in (
            ("max_charge_a",       REG_BAT_CHG_MAX,  "max_charge_a"),
            ("max_discharge_a",    REG_BAT_DIS_MAX,  "max_discharge_a"),
            ("bat2_max_charge_a",  REG_BAT2_CHG_MAX, "bat2_max_charge_a"),
            ("bat2_max_discharge_a", REG_BAT2_DIS_MAX, "bat2_max_discharge_a"),
        ):
            if key in fields:
                v = int(fields[key])
                if not 1 <= v <= 300:
                    raise ValueError(f"{label} must be 1-300")
                _write_reg(inv, reg, v)
    finally:
        inv.disconnect()


def write_general(cfg: dict, fields: dict) -> None:
    inv = _connect(cfg)
    try:
        if "grid_charge_enable" in fields:
            _write_reg(inv, REG_GRID_CHARGE_EN, 1 if fields["grid_charge_enable"] else 0)
        if "solar_sell_enable" in fields:
            _write_reg(inv, REG_SOLAR_SELL_EN, 1 if fields["solar_sell_enable"] else 0)
        if "energy_mode" in fields:
            name = fields["energy_mode"]
            if name not in ENERGY_MODES:
                raise ValueError(f"Unknown mode '{name}'. Valid: {list(ENERGY_MODES)}")
            current = _read_regs(inv, REG_ENERGY_MODE, 1)[0]
            _write_reg(inv, REG_ENERGY_MODE, (current & ~3) | ENERGY_MODES[name])
        if "max_sell_power_w" in fields:
            _write_reg(inv, REG_MAX_SELL_POWER, int(fields["max_sell_power_w"]) // 10)
        if "grid_peak_shaving_on" in fields:
            ctrl = _read_regs(inv, REG_CTRL_SPECIAL_1, 1)[0]
            if fields["grid_peak_shaving_on"]:
                ctrl = (ctrl & ~(3 << 4)) | (3 << 4)   # bits 4-5 → 11 (enable)
            else:
                ctrl = (ctrl & ~(3 << 4)) | (2 << 4)   # bits 4-5 → 10 (disable)
            _write_reg(inv, REG_CTRL_SPECIAL_1, ctrl & 0xFFFF)
        if "grid_peak_shaving_power_w" in fields:
            _write_reg(inv, REG_GRID_PS_POWER, int(fields["grid_peak_shaving_power_w"]) // 10)
    finally:
        inv.disconnect()


# ── Flask app ─────────────────────────────────────────────────────────────────

_STATIC    = Path(__file__).resolve().parent / "static"
_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
app        = Flask(__name__, static_folder=str(_STATIC), static_url_path="/static")
_cfg: dict = {}


@app.get("/")
def index():
    return send_from_directory(str(_TEMPLATES), "api-index.html")


@app.get("/api/info")
def api_info():
    return jsonify({"brand": "deye", "auto_managed": False})


def _err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


def _ok():
    return jsonify({"ok": True})


@app.get("/api/settings")
def api_get_settings():
    try:
        return jsonify(read_settings(_cfg))
    except Exception as exc:
        return _err(str(exc), 502)


@app.get("/api/status")
def api_get_status():
    try:
        result = subprocess.run(
            [sys.executable, _cfg["monitor_script"], "--format", "json"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode not in (0, 2):
            return _err(f"deye-monitor exited {result.returncode}: {result.stderr[:300]}", 502)
        return app.response_class(result.stdout, mimetype="application/json")
    except subprocess.TimeoutExpired:
        return _err("deye-monitor poll timed out", 504)
    except Exception as exc:
        return _err(str(exc), 502)


@app.post("/api/settings/tou/<int:slot_num>")
def api_post_tou(slot_num: int):
    if not 1 <= slot_num <= 6:
        return _err("slot must be 1-6")
    fields = request.get_json(silent=True) or {}
    if not fields:
        return _err("JSON body required")
    try:
        write_tou_slot(_cfg, slot_num, fields)
        return _ok()
    except (ValueError, TypeError) as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(str(exc), 502)


@app.post("/api/settings/battery")
def api_post_battery():
    fields = request.get_json(silent=True) or {}
    if not fields:
        return _err("JSON body required")
    try:
        write_battery(_cfg, fields)
        return _ok()
    except (ValueError, TypeError) as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(str(exc), 502)


@app.post("/api/settings/general")
def api_post_general():
    fields = request.get_json(silent=True) or {}
    if not fields:
        return _err("JSON body required")
    try:
        write_general(_cfg, fields)
        return _ok()
    except Exception as exc:
        return _err(str(exc), 502)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _cfg
    parser = argparse.ArgumentParser(description="Deye management HTTP API")
    parser.add_argument("--config", default=str(_DEFAULT_CONFIG))
    parser.add_argument("--host",   default=None)
    parser.add_argument("--port",   type=int, default=None)
    parser.add_argument("--debug",  action="store_true")
    args = parser.parse_args()

    try:
        _cfg = load_config(Path(args.config))
    except Exception as exc:
        sys.exit(f"Config error: {exc}")

    host = args.host or _cfg["server_host"]
    port = args.port or _cfg["server_port"]
    print(f"Deye API → {_cfg['ip']}:{_cfg['port']}  (sn {_cfg['sn']})", flush=True)
    app.run(host=host, port=port, debug=args.debug)


if __name__ == "__main__":
    main()
