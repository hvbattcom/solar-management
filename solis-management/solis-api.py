#!/usr/bin/env python3
"""
solis-api.py — Flask HTTP API for writing Solis inverter settings over Modbus.

Reads:
  GET /api/settings  — storage + TOU holding registers (fast)
  GET /api/status    — full inverter poll via solis-monitor.py --format json (slow)

Writes (partial update — only fields present in the body are changed):
  POST /api/settings/storage
  POST /api/settings/tou/charge/<1-6>
  POST /api/settings/tou/discharge/<1-6>

All write responses: {"ok": true} or {"ok": false, "error": "..."}
"""

import argparse
import configparser
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from pymodbus.client import ModbusTcpClient

# ── Constants (mirrors solis-monitor.py) ─────────────────────────────────────

STORAGE_MODE_BITS = {
    0:  "Self Use",
    1:  "Time of Use",
    2:  "Off Grid",
    6:  "Feed In Priority",
    11: "Peak Shaving",
}
STORAGE_MODE_BY_NAME = {v: k for k, v in STORAGE_MODE_BITS.items()}

# Storage registers
REG_BATTERY_RESERVE_PCT = 43024   # raw = percent
REG_MAX_EXPORT_POWER    = 43074   # raw × 100 = watts
REG_MODE_BITMASK        = 43110   # multi-bit: mode + reserve_on + grid_charge
REG_HYBRID_CTRL         = 43483   # bit 3 = allow_export

# Bit positions within REG_MODE_BITMASK
BIT_BATTERY_RESERVE_ON  = 4
BIT_ALLOW_GRID_CHARGE   = 5
MODE_BITS               = frozenset(STORAGE_MODE_BITS.keys())   # bits 0,1,2,6,11

# TOU registers
REG_TOU_SWITCH          = 43707   # bitmask: bits 0-5 = charge slots 1-6, bits 6-11 = discharge
REG_TOU_CHARGE_BASE     = 43708   # first charge slot; 7 regs per slot
REG_TOU_DISCHARGE_BASE  = 43750   # first discharge slot; 7 regs per slot
TOU_SLOT_FIELDS         = 7       # SOC%, current×10, cutoff×10, start_h, start_m, end_h, end_m
TOU_CURRENT_SCALE       = 10      # register value = amps × 10
TOU_VOLTAGE_SCALE       = 10      # register value = volts × 10

# ── Config loading ────────────────────────────────────────────────────────────

_DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "solis-monitor" / "config.cfg"

def load_config(path: Path) -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(str(path))
    sec = cfg["SolisInverter"]
    use_zb = sec.get("use_zero_based_addressing", "false").strip().lower() == "true"
    srv = cfg["SolisAPI"] if "SolisAPI" in cfg else {}
    return {
        "ip":             sec["inverter_ip"].strip(),
        "port":           int(sec.get("inverter_port", 502)),
        "slave_id":       int(sec.get("slave_id", 1)),
        "use_zero_based": use_zb,
        "monitor_script": str(Path(__file__).resolve().parent.parent / "solis-monitor" / "solis-monitor.py"),
        "server_host":    srv.get("host", "127.0.0.1").strip(),
        "server_port":    int(srv.get("port", 5000)),
    }

# ── Modbus helpers ────────────────────────────────────────────────────────────

def _modbus_connect(cfg: dict) -> ModbusTcpClient:
    client = ModbusTcpClient(cfg["ip"], port=cfg["port"], timeout=5, retries=2)
    if client.connect():
        return client
    time.sleep(1.0)
    client.close()
    client = ModbusTcpClient(cfg["ip"], port=cfg["port"], timeout=5, retries=2)
    if client.connect():
        return client
    raise ConnectionError(f"Cannot connect to {cfg['ip']}:{cfg['port']}")

def _addr(cfg: dict, register: int) -> int:
    return register - 1 if cfg["use_zero_based"] else register

def _read_reg(client: ModbusTcpClient, cfg: dict, register: int) -> int:
    """FC3: read a single holding register. Raises on error."""
    rr = client.read_holding_registers(address=_addr(cfg, register), count=1, device_id=cfg["slave_id"])
    if rr.isError():
        raise IOError(f"Read register {register} failed: {rr}")
    return rr.registers[0]

def _write_reg(client: ModbusTcpClient, cfg: dict, register: int, value: int) -> None:
    """FC6: write a single holding register. Raises on error."""
    rr = client.write_register(address=_addr(cfg, register), value=int(value), device_id=cfg["slave_id"])
    if rr.isError():
        raise IOError(f"Write register {register}={value} failed: {rr}")

def _write_regs(client: ModbusTcpClient, cfg: dict, register: int, values: list) -> None:
    """FC16: write multiple consecutive holding registers. Raises on error."""
    rr = client.write_registers(address=_addr(cfg, register), values=[int(v) for v in values], device_id=cfg["slave_id"])
    if rr.isError():
        raise IOError(f"Write registers {register}+{len(values)} failed: {rr}")

def _read_block(client: ModbusTcpClient, cfg: dict, start: int, count: int) -> list:
    """FC3: read `count` consecutive holding registers starting at `start`. Raises on error."""
    rr = client.read_holding_registers(address=_addr(cfg, start), count=count, device_id=cfg["slave_id"])
    if rr.isError():
        raise IOError(f"Read block {start}+{count} failed: {rr}")
    return rr.registers

# ── Validation helpers ────────────────────────────────────────────────────────

def _parse_time(t: str) -> tuple:
    """Parse 'HH:MM' → (hour, minute). Raises ValueError on bad input."""
    parts = str(t).split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time '{t}', expected HH:MM")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Time '{t}' out of range")
    return h, m

def _validate_storage(fields: dict) -> None:
    if "mode" in fields and fields["mode"] not in STORAGE_MODE_BY_NAME:
        raise ValueError(f"Unknown mode '{fields['mode']}'. Valid: {list(STORAGE_MODE_BY_NAME)}")
    if "battery_reserve_pct" in fields:
        v = int(fields["battery_reserve_pct"])
        if not 0 <= v <= 100:
            raise ValueError("battery_reserve_pct must be 0-100")
    if "max_export_power_w" in fields:
        v = int(fields["max_export_power_w"])
        if v < 0 or v % 100 != 0:
            raise ValueError("max_export_power_w must be a non-negative multiple of 100")

def _validate_tou_slot(fields: dict) -> None:
    if "start" in fields:
        _parse_time(fields["start"])
    if "end" in fields:
        _parse_time(fields["end"])
    if "current_a" in fields and float(fields["current_a"]) < 0:
        raise ValueError("current_a must be >= 0")
    if "cutoff_v" in fields and float(fields["cutoff_v"]) < 0:
        raise ValueError("cutoff_v must be >= 0")
    if "soc_pct" in fields:
        v = int(fields["soc_pct"])
        if not 0 <= v <= 100:
            raise ValueError("soc_pct must be 0-100")

# ── High-level write functions ────────────────────────────────────────────────

def write_storage_settings(client: ModbusTcpClient, cfg: dict, fields: dict) -> None:
    """Apply any subset of storage settings. Reads 43110 once, modifies all needed bits, writes once."""
    # Direct single-register writes first
    if "battery_reserve_pct" in fields:
        _write_reg(client, cfg, REG_BATTERY_RESERVE_PCT, int(fields["battery_reserve_pct"]))

    if "max_export_power_w" in fields:
        _write_reg(client, cfg, REG_MAX_EXPORT_POWER, int(fields["max_export_power_w"]) // 100)

    # REG_MODE_BITMASK (43110): mode bits + reserve_on + grid_charge — one RMW for all
    bitmask_43110_fields = {"mode", "battery_reserve_on", "allow_grid_charge"}
    if bitmask_43110_fields & fields.keys():
        current = _read_reg(client, cfg, REG_MODE_BITMASK)
        if "mode" in fields:
            for bit in MODE_BITS:
                current &= ~(1 << bit)          # clear all mode bits
            current |= 1 << STORAGE_MODE_BY_NAME[fields["mode"]]
        if "battery_reserve_on" in fields:
            if fields["battery_reserve_on"]:
                current |= 1 << BIT_BATTERY_RESERVE_ON
            else:
                current &= ~(1 << BIT_BATTERY_RESERVE_ON)
        if "allow_grid_charge" in fields:
            if fields["allow_grid_charge"]:
                current |= 1 << BIT_ALLOW_GRID_CHARGE
            else:
                current &= ~(1 << BIT_ALLOW_GRID_CHARGE)
        _write_reg(client, cfg, REG_MODE_BITMASK, current)

    # REG_HYBRID_CTRL (43483): allow_export bit 3
    if "allow_export" in fields:
        current = _read_reg(client, cfg, REG_HYBRID_CTRL)
        if fields["allow_export"]:
            current &= ~(1 << 3)
        else:
            current |= 1 << 3
        _write_reg(client, cfg, REG_HYBRID_CTRL, current)


def write_tou_slot(client: ModbusTcpClient, cfg: dict, slot_type: str, slot_num: int, fields: dict) -> None:
    """
    Update a single TOU slot. slot_type: 'charge' or 'discharge'. slot_num: 1-6.
    Only fields present in `fields` are changed; others are read-back from the inverter.
    """
    base = (REG_TOU_CHARGE_BASE if slot_type == "charge" else REG_TOU_DISCHARGE_BASE) + (slot_num - 1) * TOU_SLOT_FIELDS

    # Read existing slot registers for fields not being overwritten
    existing = {}
    rr = client.read_holding_registers(address=_addr(cfg, base), count=TOU_SLOT_FIELDS, device_id=cfg["slave_id"])
    if rr.isError():
        raise IOError(f"Read TOU slot {slot_type} {slot_num} failed: {rr}")
    ex = rr.registers  # [soc_pct, current×10, cutoff×10, start_h, start_m, end_h, end_m]

    soc_pct   = int(fields["soc_pct"])            if "soc_pct"   in fields else ex[0]
    current   = round(float(fields["current_a"]) * TOU_CURRENT_SCALE) if "current_a" in fields else ex[1]
    cutoff    = round(float(fields["cutoff_v"])  * TOU_VOLTAGE_SCALE) if "cutoff_v"  in fields else ex[2]
    if "start" in fields:
        start_h, start_m = _parse_time(fields["start"])
    else:
        start_h, start_m = ex[3], ex[4]
    if "end" in fields:
        end_h, end_m = _parse_time(fields["end"])
    else:
        end_h, end_m = ex[5], ex[6]

    _write_regs(client, cfg, base, [soc_pct, current, cutoff, start_h, start_m, end_h, end_m])

    # Update enable bitmask (REG_TOU_SWITCH) if requested
    if "enabled" in fields:
        switch = _read_reg(client, cfg, REG_TOU_SWITCH)
        bit = (slot_num - 1) if slot_type == "charge" else (6 + slot_num - 1)
        if fields["enabled"]:
            switch |= 1 << bit
        else:
            switch &= ~(1 << bit)
        _write_reg(client, cfg, REG_TOU_SWITCH, switch)

# ── Read helpers ─────────────────────────────────────────────────────────────

def read_settings(client: ModbusTcpClient, cfg: dict) -> dict:
    """Read all storage + TOU settings in 3 block reads instead of ~17 individual reads."""
    # Block 1: 43024–43110 (87 regs) — reserve_pct, max_export_raw, mode_mask
    blk1           = _read_block(client, cfg, REG_BATTERY_RESERVE_PCT, REG_MODE_BITMASK - REG_BATTERY_RESERVE_PCT + 1)
    reserve_pct    = blk1[0]
    max_export_raw = blk1[REG_MAX_EXPORT_POWER    - REG_BATTERY_RESERVE_PCT]  # offset 50
    mode_mask      = blk1[REG_MODE_BITMASK        - REG_BATTERY_RESERVE_PCT]  # offset 86

    # Block 2: 43483 (1 reg) — hybrid control / allow_export
    hybrid_ctrl = _read_block(client, cfg, REG_HYBRID_CTRL, 1)[0]

    mode = next((name for bit, name in sorted(STORAGE_MODE_BITS.items(), reverse=True) if (mode_mask >> bit) & 1), "Unknown")
    storage = {
        "mode":                mode,
        "battery_reserve_on":  bool((mode_mask >> BIT_BATTERY_RESERVE_ON) & 1),
        "battery_reserve_pct": reserve_pct,
        "allow_grid_charge":   bool((mode_mask >> BIT_ALLOW_GRID_CHARGE) & 1),
        "allow_export":        not bool((hybrid_ctrl >> 3) & 1),
        "max_export_power_w":  max_export_raw * 100,
    }

    # Block 3: 43707–43791 (85 regs) — TOU switch + all 12 slots
    # Layout: [0]=switch, [1..42]=charge slots 1-6 (7 regs each), [43..84]=discharge slots 1-6
    _TOU_BLOCK_END = REG_TOU_DISCHARGE_BASE + 6 * TOU_SLOT_FIELDS  # 43792, exclusive
    try:
        blk3   = _read_block(client, cfg, REG_TOU_SWITCH, _TOU_BLOCK_END - REG_TOU_SWITCH)
        switch = blk3[0]

        def _decode_slot(slot_type: str, slot_num: int) -> dict:
            base = (REG_TOU_CHARGE_BASE if slot_type == "charge" else REG_TOU_DISCHARGE_BASE) + (slot_num - 1) * TOU_SLOT_FIELDS
            off  = base - REG_TOU_SWITCH
            bit  = (slot_num - 1) if slot_type == "charge" else (6 + slot_num - 1)
            r    = blk3[off:off + TOU_SLOT_FIELDS]
            return {
                "slot":      slot_num,
                "enabled":   bool((switch >> bit) & 1),
                "start":     f"{r[3]:02d}:{r[4]:02d}",
                "end":       f"{r[5]:02d}:{r[6]:02d}",
                "current_a": r[1] / TOU_CURRENT_SCALE,
                "cutoff_v":  r[2] / TOU_VOLTAGE_SCALE,
                "soc_pct":   r[0],
            }

        tou = {
            "enabled":         bool(switch),
            "charge_slots":    [_decode_slot("charge",    i) for i in range(1, 7)],
            "discharge_slots": [_decode_slot("discharge", i) for i in range(1, 7)],
        }
    except IOError:
        tou = None

    return {"storage": storage, "tou": tou}

# ── Flask application ─────────────────────────────────────────────────────────

_STATIC   = Path(__file__).resolve().parent / "static"
_MAPS_DIR = Path(__file__).resolve().parent / "maps"
_MAPS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=str(_STATIC), static_url_path="/static")
_cfg: dict = {}   # populated at startup

@app.get("/")
def index():
    return send_from_directory(str(_STATIC), "index.html")

def _err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code

def _ok():
    return jsonify({"ok": True})

@app.get("/api/settings")
def api_get_settings():
    try:
        client = _modbus_connect(_cfg)
        try:
            data = read_settings(client, _cfg)
        finally:
            client.close()
        return jsonify(data)
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
            return _err(f"solis-monitor exited {result.returncode}: {result.stderr[:300]}", 502)
        return app.response_class(result.stdout, mimetype="application/json")
    except subprocess.TimeoutExpired:
        return _err("solis-monitor poll timed out", 504)
    except Exception as exc:
        return _err(str(exc), 502)

@app.post("/api/settings/storage")
def api_post_storage():
    fields = request.get_json(silent=True) or {}
    if not fields:
        return _err("JSON body required")
    try:
        _validate_storage(fields)
    except ValueError as exc:
        return _err(str(exc))
    try:
        client = _modbus_connect(_cfg)
        try:
            write_storage_settings(client, _cfg, fields)
        finally:
            client.close()
        return _ok()
    except Exception as exc:
        return _err(str(exc), 502)

@app.post("/api/settings/tou/<slot_type>/<int:slot_num>")
def api_post_tou(slot_type: str, slot_num: int):
    if slot_type not in ("charge", "discharge"):
        return _err("slot_type must be 'charge' or 'discharge'")
    if not 1 <= slot_num <= 6:
        return _err("slot_num must be 1-6")
    fields = request.get_json(silent=True) or {}
    if not fields:
        return _err("JSON body required")
    try:
        _validate_tou_slot(fields)
    except ValueError as exc:
        return _err(str(exc))
    try:
        client = _modbus_connect(_cfg)
        try:
            write_tou_slot(client, _cfg, slot_type, slot_num, fields)
        finally:
            client.close()
        return _ok()
    except Exception as exc:
        return _err(str(exc), 502)

# ── Dispatch map endpoints ────────────────────────────────────────────────────

@app.post("/api/map")
def api_map_receive():
    """Receive a dispatch map from the solar planner and store it locally."""
    data = request.get_json(silent=True)
    if not data or "date" not in data or "instance_id" not in data:
        return _err("JSON body with 'date' and 'instance_id' required")
    path = _MAPS_DIR / f"map_{data['date']}_{data['instance_id']}.json"
    path.write_text(json.dumps(data, indent=2))
    print(f"[map] received {path.name} ({len(data.get('segments', []))} segments)", flush=True)
    return _ok()


@app.get("/api/map/current")
def api_map_current():
    """Return the current 15-min slot from today's dispatch map."""
    instance_id = request.args.get("instance_id", "")
    today = datetime.now().strftime("%Y-%m-%d")
    path  = _MAPS_DIR / f"map_{today}_{instance_id}.json"
    if not path.exists():
        # Fall back to most recent map for this instance
        candidates = sorted(_MAPS_DIR.glob(f"map_*_{instance_id}.json"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return jsonify({"error": "no map found"}), 404
        path = candidates[0]
    data     = json.loads(path.read_text())
    now      = datetime.now()
    now_min  = now.hour * 60 + now.minute
    segments = data.get("segments", [])
    current  = next((s for s in segments
                     if int(s["start"][:2]) * 60 + int(s["start"][3:]) <= now_min
                     <  int(s["end"][:2])   * 60 + int(s["end"][3:])),   None)
    if current is None:
        return jsonify({"error": "no segment for current time"}), 404
    return jsonify({**current, "map_date": data.get("date"), "map_algo": data.get("algo")})


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _cfg
    parser = argparse.ArgumentParser(description="Solis management HTTP API")
    parser.add_argument("--config", default=str(_DEFAULT_CONFIG),
                        help="Path to config.cfg (default: ../solis-monitor/config.cfg)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    try:
        _cfg = load_config(Path(args.config))
    except Exception as exc:
        sys.exit(f"Config error: {exc}")

    host = args.host if args.host != "127.0.0.1" else _cfg["server_host"]
    port = args.port if args.port != 5000       else _cfg["server_port"]
    print(f"Solis API → {_cfg['ip']}:{_cfg['port']}  (slave {_cfg['slave_id']})", flush=True)
    app.run(host=host, port=port, debug=args.debug)

if __name__ == "__main__":
    main()
