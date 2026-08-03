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
  POST /api/settings/tou/all      — update all 6 TOU slots in one connection
  POST /api/settings/battery      — battery max charge/discharge current
  POST /api/settings/general      — grid charge enable, solar sell enable, etc.

Dispatch map endpoints (fed by the solar planner, consumed by dispatcher.py):
  POST /api/map              — receive a dispatch map
  GET  /api/map/current      — current 15-min segment of today's map
  GET/POST /api/auto-managed — auto-management flag (blocks manual writes)
"""

import argparse
import configparser
import importlib.util
import json
import sys
from datetime import datetime
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
REG_TOU_ENABLE      = 146   # bit 0 = TOU schedule on, bits 1-7 = Mon..Sun
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

BIT_TOU_ON   = 0            # within REG_TOU_ENABLE
TOU_ALL_DAYS = 0xFE         # bits 1-7 set = schedule runs every weekday

# The 30-register TOU block starting at REG_TOU_TIME_BASE, by field offset:
#   [0:6]=time  [6:12]=power  [12:18]=voltage (unused)  [18:24]=soc  [24:30]=ctrl
TOU_BLOCK_LEN  = 30
TOU_OFF_TIME   = 0
TOU_OFF_POWER  = 6
TOU_OFF_SOC    = 18
TOU_OFF_CTRL   = 24

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
    cfg["server_port"] = int(srv.get("port", 5000))
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
        # 145 (solar sell) and 146 (TOU enable) are adjacent — one round trip.
        # Every extra request widens the window for a collision with the
        # Prometheus scrape, since the datalogger serves one connection at a time.
        blk145  = _read_regs(inv, REG_SOLAR_SELL_EN, 2)
        ssen, tou_en = blk145[0], blk145[1]
        tou_blk = _read_regs(inv, REG_TOU_TIME_BASE, TOU_BLOCK_LEN)
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
        ctrl_raw = tou_blk[TOU_OFF_CTRL + i]
        slots.append({
            "slot":        i + 1,
            "time":        _hhmm_to_str(tou_blk[TOU_OFF_TIME + i]),
            "power_w":     tou_blk[TOU_OFF_POWER + i] * 10,
            "soc_pct":     tou_blk[TOU_OFF_SOC + i],
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
        "tou": {
            "enabled":  bool(tou_en & (1 << BIT_TOU_ON)),
            "days_mask": tou_en & TOU_ALL_DAYS,
            "slots":    slots,
        },
    }


# ── High-level writes ─────────────────────────────────────────────────────────

def _apply_slot_fields(blk: list, slot_num: int, fields: dict) -> None:
    """Fold one slot's requested fields into an in-memory copy of the 30-reg TOU block.

    Fields absent from `fields` keep whatever the inverter already had, so a
    partial update never clobbers a setting the caller did not mention."""
    i = slot_num - 1
    if "time" in fields:
        blk[TOU_OFF_TIME + i] = _str_to_hhmm(fields["time"])
    if "power_w" in fields:
        blk[TOU_OFF_POWER + i] = int(int(fields["power_w"]) // 10)
    if "soc_pct" in fields:
        v = int(fields["soc_pct"])
        if not 0 <= v <= 100:
            raise ValueError("soc_pct must be 0-100")
        blk[TOU_OFF_SOC + i] = v

    ctrl = blk[TOU_OFF_CTRL + i]
    for bit, key in ((BIT_GRID_CHG, "grid_charge"), (BIT_GEN_CHG, "gen_charge"), (BIT_SELL, "sell")):
        if key in fields:
            if fields[key]:
                ctrl |= 1 << bit
            else:
                ctrl &= ~(1 << bit)
    blk[TOU_OFF_CTRL + i] = ctrl & 0xFFFF


def _flush_block(inv, before: list, after: list) -> int:
    """Write back only the registers that actually changed. Returns how many.

    The datalogger takes one round-trip per write and every write is an EEPROM
    cycle, so a no-op re-write of all 30 registers on every 5-minute dispatcher
    run would be both slow and needless wear."""
    written = 0
    for off, (old, new) in enumerate(zip(before, after)):
        if old != new:
            _write_reg(inv, REG_TOU_TIME_BASE + off, new)
            written += 1
    return written


def write_tou_slot(cfg: dict, slot_num: int, fields: dict) -> None:
    if not 1 <= slot_num <= 6:
        raise ValueError("slot must be 1-6")
    inv = _connect(cfg)
    try:
        before = _read_regs(inv, REG_TOU_TIME_BASE, TOU_BLOCK_LEN)
        after  = list(before)
        _apply_slot_fields(after, slot_num, fields)
        _flush_block(inv, before, after)
    finally:
        inv.disconnect()


def write_all_tou_slots(cfg: dict, slot_updates: list) -> int:
    """Update any number of TOU slots over a single datalogger connection.

    Deye's schedule is a partition of the day — slot N runs from its own start
    time until slot N+1's (slot 6 wraps to slot 1) — so slots are never written
    in isolation by the dispatcher: shifting one boundary changes the length of
    its neighbour. Reading once, folding every update in, then writing only the
    differing registers keeps the whole reshape to one connection and the
    minimum number of EEPROM cycles."""
    inv = _connect(cfg)
    try:
        before = _read_regs(inv, REG_TOU_TIME_BASE, TOU_BLOCK_LEN)
        after  = list(before)
        for fields in slot_updates:
            _apply_slot_fields(after, int(fields["slot"]), fields)
        return _flush_block(inv, before, after)
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
        if "tou_enabled" in fields:
            # Enabling also arms every weekday: the schedule is regenerated daily,
            # so a day the mask skipped would silently fall back to plain self-use.
            cur = _read_regs(inv, REG_TOU_ENABLE, 1)[0]
            if fields["tou_enabled"]:
                new = (cur | (1 << BIT_TOU_ON) | TOU_ALL_DAYS)
            else:
                new = cur & ~(1 << BIT_TOU_ON)
            if new != cur:
                _write_reg(inv, REG_TOU_ENABLE, new & 0xFFFF)
    finally:
        inv.disconnect()


# ── Flask app ─────────────────────────────────────────────────────────────────

_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
_MAPS_DIR  = Path(__file__).resolve().parent / "maps"
_AUTO_FILE = Path(__file__).resolve().parent / "auto_managed.json"
_MAPS_DIR.mkdir(exist_ok=True)

app        = Flask(__name__)
_cfg: dict = {}


def _auto_managed() -> bool:
    if _AUTO_FILE.exists():
        return json.loads(_AUTO_FILE.read_text()).get("enabled", True)
    return True


@app.get("/")
def index():
    return send_from_directory(str(_TEMPLATES), "api-index.html")


@app.get("/status")
def status_page():
    return send_from_directory(str(_TEMPLATES), "status.html")


@app.get("/api/info")
def api_info():
    return jsonify({"brand": "deye", "auto_managed": _auto_managed()})


def _err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


def _ok(**extra):
    return jsonify({"ok": True, **extra})


def _managed_guard():
    """Return 403 if auto-management is active and the caller is not the dispatcher."""
    if not _auto_managed():
        return None
    if request.headers.get("X-Dispatcher") == "1":
        return None  # identified as the automated dispatcher — always allowed
    return _err("auto-management is active — manual writes are blocked", 403)


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
    if (g := _managed_guard()): return g
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


@app.post("/api/settings/tou/all")
def api_post_tou_all():
    """Reshape the whole schedule in one connection.

    Body: [{slot: 1-6, time?, power_w?, soc_pct?, grid_charge?, gen_charge?, sell?}, ...]"""
    if (g := _managed_guard()): return g
    slot_updates = request.get_json(silent=True)
    if not isinstance(slot_updates, list) or not slot_updates:
        return _err("JSON array of slot objects required")
    for item in slot_updates:
        if not isinstance(item, dict) or "slot" not in item:
            return _err("each item must have a 'slot' field")
        if not 1 <= int(item["slot"]) <= 6:
            return _err(f"slot must be 1-6, got {item['slot']}")
        if "time" in item:
            try:
                _str_to_hhmm(item["time"])
            except ValueError as exc:
                return _err(str(exc))
    try:
        written = write_all_tou_slots(_cfg, slot_updates)
        return _ok(registers_written=written)
    except (ValueError, TypeError) as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(str(exc), 502)


@app.post("/api/settings/battery")
def api_post_battery():
    if (g := _managed_guard()): return g
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
    if (g := _managed_guard()): return g
    fields = request.get_json(silent=True) or {}
    if not fields:
        return _err("JSON body required")
    try:
        write_general(_cfg, fields)
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


# ── Auto-managed flag ─────────────────────────────────────────────────────────

@app.get("/api/auto-managed")
def api_get_auto_managed():
    return jsonify({"enabled": _auto_managed()})


@app.post("/api/auto-managed")
def api_set_auto_managed():
    data    = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    _AUTO_FILE.write_text(json.dumps({"enabled": enabled}))
    print(f"[auto-managed] {'enabled' if enabled else 'disabled'}", flush=True)
    return _ok()


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
