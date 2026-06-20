#!/usr/bin/env python3
"""
deye-api.py — Flask HTTP API for Deye inverter settings and live status.

Read endpoints:
  GET /          — management UI (templates/api-index.html)
  GET /status    — live status dashboard (templates/status.html)
  GET /api/info  — brand + feature flags (instant, hardcoded)
  GET /api/settings  — TOU + battery + general holding registers
  GET /api/status    — full live poll as JSON
  GET /metrics       — Prometheus text format
  GET /human         — human-readable text (terminal format)

Write endpoints (partial — only fields present in the body are changed):
  POST /api/settings/tou/<1-6>    — update a single TOU slot
  POST /api/settings/battery      — battery max charge/discharge current
  POST /api/settings/general      — grid charge enable, solar sell enable, etc.
"""

import argparse
import configparser
import importlib.util
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

# ── Load deye-monitor as a module (hyphen in filename requires importlib) ──────
_monitor_path = Path(__file__).resolve().parent / "deye-monitor.py"
_spec         = importlib.util.spec_from_file_location("deye_monitor", str(_monitor_path))
_monitor      = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_monitor)

try:
    from pysolarmanv5 import PySolarmanV5
except ImportError:
    PySolarmanV5 = None

# ── Register map (write endpoints only – full map lives in deye-monitor.py) ───

REG_BAT_CHG_MAX     = 108
REG_BAT_DIS_MAX     = 109
REG_GRID_CHARGE_EN  = 130
REG_ENERGY_MODE     = 140
REG_MAX_SELL_POWER  = 143
REG_SOLAR_SELL_EN   = 145
REG_TOU_TIME_BASE   = 148
REG_TOU_POWER_BASE  = 154
REG_TOU_SOC_BASE    = 166
REG_TOU_CTRL_BASE   = 172
REG_CTRL_SPECIAL_1  = 178
REG_GRID_PS_POWER   = 191
REG_BAT2_CHG_MAX    = 243
REG_BAT2_DIS_MAX    = 244

BIT_GRID_CHG = 0
BIT_GEN_CHG  = 1
BIT_SELL     = 5

ENERGY_MODES         = {"Self Use": 0, "Selling First": 1, "Battery First": 2, "Load First": 3}
ENERGY_MODES_BY_CODE = {v: k for k, v in ENERGY_MODES.items()}

REG_LIMIT_CONTROL         = 142   # 0=off, 1=zero export to load, 2=zero export to CT
LIMIT_CONTROL             = {"Selling First": 0, "Zero Export to Load": 1, "Zero Export to CT": 2}
LIMIT_CONTROL_BY_CODE     = {v: k for k, v in LIMIT_CONTROL.items()}

# ── Config ────────────────────────────────────────────────────────────────────

_DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.cfg"


def load_config(path: Path) -> dict:
    cfg = _monitor.load_config(path)   # ip, port, sn, brand, verbose, inverter_power_w, mppt_count
    parser = configparser.ConfigParser()
    parser.read(str(path))
    srv = parser["DeyeAPI"] if "DeyeAPI" in parser else {}
    cfg["server_host"] = srv.get("host", "0.0.0.0").strip()
    cfg["server_port"] = int(srv.get("port", 5001))
    return cfg


# ── SolarmanV5 helpers (write path) ──────────────────────────────────────────

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


# ── High-level read (settings panel) ─────────────────────────────────────────

def read_settings(cfg: dict) -> dict:
    inv = _connect(cfg)
    try:
        bat1    = _read_regs(inv, REG_BAT_CHG_MAX, 2)
        gcen    = _read_regs(inv, REG_GRID_CHARGE_EN, 1)[0]
        blk140  = _read_regs(inv, REG_ENERGY_MODE, 4)
        ssen    = _read_regs(inv, REG_SOLAR_SELL_EN, 1)[0]
        tou_blk = _read_regs(inv, REG_TOU_TIME_BASE, 30)
        blk178  = _read_regs(inv, REG_CTRL_SPECIAL_1, 14)
        bat2    = _read_regs(inv, REG_BAT2_CHG_MAX, 2)
    finally:
        inv.disconnect()

    mode_code    = blk140[0] & 3
    limit_val    = blk140[2]          # reg 142 — limit control
    max_sell_raw = blk140[3]
    ctrl178      = blk178[0]
    grid_ps_raw  = blk178[13]
    grid_ps_on   = ((ctrl178 >> 4) & 3) == 3

    slots = []
    for i in range(6):
        ctrl_raw = tou_blk[24 + i]
        slots.append({
            "slot":        i + 1,
            "time":        _hhmm_to_str(tou_blk[i]),
            "power_w":     tou_blk[6 + i] * 10,
            "soc_pct":     tou_blk[18 + i],
            "grid_charge": bool(ctrl_raw & (1 << BIT_GRID_CHG)),
            "gen_charge":  bool(ctrl_raw & (1 << BIT_GEN_CHG)),
            "sell":        bool(ctrl_raw & (1 << BIT_SELL)),
        })

    return {
        "general": {
            "energy_mode":               ENERGY_MODES_BY_CODE.get(mode_code, "Self Use"),
            "limit_control":             LIMIT_CONTROL_BY_CODE.get(limit_val, "Selling First"),
            "grid_charge_enable":        bool(gcen),
            "solar_sell_enable":         bool(ssen),
            "max_sell_power_w":          max_sell_raw * 10,
            "grid_peak_shaving_on":      grid_ps_on,
            "grid_peak_shaving_power_w": grid_ps_raw * 10,
        },
        "battery": {
            "max_charge_a":         bat1[0],
            "max_discharge_a":      bat1[1],
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
        blk       = _read_regs(inv, REG_TOU_TIME_BASE, 30)
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
            ("max_charge_a",         REG_BAT_CHG_MAX,  "max_charge_a"),
            ("max_discharge_a",      REG_BAT_DIS_MAX,  "max_discharge_a"),
            ("bat2_max_charge_a",    REG_BAT2_CHG_MAX, "bat2_max_charge_a"),
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
                ctrl = (ctrl & ~(3 << 4)) | (3 << 4)
            else:
                ctrl = (ctrl & ~(3 << 4)) | (2 << 4)
            _write_reg(inv, REG_CTRL_SPECIAL_1, ctrl & 0xFFFF)
        if "grid_peak_shaving_power_w" in fields:
            _write_reg(inv, REG_GRID_PS_POWER, int(fields["grid_peak_shaving_power_w"]) // 10)
        if "limit_control" in fields:
            name = fields["limit_control"]
            if name not in LIMIT_CONTROL:
                raise ValueError(f"Unknown limit control '{name}'. Valid: {list(LIMIT_CONTROL)}")
            _write_reg(inv, REG_LIMIT_CONTROL, LIMIT_CONTROL[name])
    finally:
        inv.disconnect()


# ── Flask app ─────────────────────────────────────────────────────────────────

_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
app        = Flask(__name__)
_cfg: dict = {}


@app.get("/")
def index():
    return send_from_directory(str(_TEMPLATES), "api-index.html")


@app.get("/status")
def status_page():
    return send_from_directory(str(_TEMPLATES), "status.html")


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
        ctx = _monitor.poll(_cfg)
        return app.response_class(
            _monitor.render_to_str("json", ctx),
            mimetype="application/json",
        )
    except Exception as exc:
        return _err(str(exc), 502)


@app.get("/metrics")
def api_metrics():
    try:
        ctx = _monitor.poll(_cfg)
        return app.response_class(
            _monitor.render_to_str("prometheus", ctx),
            mimetype="text/plain; version=0.0.4",
        )
    except Exception as exc:
        return _err(str(exc), 502)


@app.get("/human")
def api_human():
    try:
        ctx = _monitor.poll(_cfg)
        return app.response_class(
            _monitor.render_to_str("human", ctx),
            mimetype="text/plain; charset=utf-8",
        )
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
