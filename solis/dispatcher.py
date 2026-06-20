#!/usr/bin/env python3
"""
dispatcher.py — Solar plan event dispatcher (5-min cron).

Each run:
  1. Derive desired export + charge state from all past map events
  2. Sync TOU discharge slots (randomised slot assignment to spread EEPROM wear)
  3. Apply export / charge_amps if different from firmware
  4. SoC guard: if inside a TOU discharge window and SoC ≤ floor + 2% → disable slot

All amps values split equally across both batteries (÷2, min 1 A each).

Crontab:  */5 * * * *  /usr/bin/python3 /path/to/dispatcher.py
"""

import argparse
import configparser
import hashlib
import json
import logging
import random
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


_MAPS_DIR    = Path(__file__).resolve().parent / "maps"
_STATE_FILE  = Path(__file__).resolve().parent / "dispatcher_state.json"
_LOG_FILE    = Path(__file__).resolve().parent / "dispatcher.log"
_DEFAULT_CFG = Path(__file__).resolve().parent / "config.cfg"
EVENT_TOLERANCE_MIN = 3


# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_log() -> logging.Logger:
    logger = logging.getLogger("dispatcher")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh  = RotatingFileHandler(_LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh  = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger

log = _setup_log()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tm(t: str) -> int:
    if t == "24:00": return 1440
    h, m = t.split(":"); return int(h) * 60 + int(m)

def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"

def _tou_t(t: str) -> str:
    return "00:00" if t == "24:00" else t

def _per_bat(amps: float) -> float:
    return max(1.0, round(amps / 2, 1))


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(str(path))
    srv = cfg["SolisAPI"] if "SolisAPI" in cfg else {}
    return {
        "api_url":  f"http://localhost:{srv.get('port', 5000)}",
        "prom_url": srv.get("mothership_prometheus_api", "").rstrip("/"),
    }


# ── Map ───────────────────────────────────────────────────────────────────────

def load_map(instance_id: str | None = None) -> dict:
    today    = date.today().isoformat()
    pattern  = f"map_{today}_{instance_id}.json" if instance_id else f"map_{today}_*.json"
    candidates = sorted(_MAPS_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        candidates = sorted(_MAPS_DIR.glob("map_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            log.error("no map found in %s", _MAPS_DIR); sys.exit(1)
        log.warning("no map for today — using %s", candidates[0].name)
    m = json.loads(candidates[0].read_text())
    log.info("map %s: %d events, %d tou_slots", candidates[0].name,
             len(m.get("events", [])), len(m.get("tou_slots", [])))
    return m


# ── Desired state ─────────────────────────────────────────────────────────────

def desired_state(events: list, now_min: int) -> dict:
    """Walk events up to now+tolerance; return last value of each field seen."""
    state: dict = {}
    for ev in events:
        if _tm(ev["time"]) > now_min + EVENT_TOLERANCE_MIN:
            break
        if "export"      in ev: state["export"]      = ev["export"]
        if "charge_amps" in ev: state["charge_amps"] = ev["charge_amps"]
    return state


# ── Firmware read ─────────────────────────────────────────────────────────────

def read_fw(api_url: str) -> dict:
    with urllib.request.urlopen(f"{api_url}/api/settings", timeout=10) as r:
        return json.loads(r.read())


# ── HTTP post ─────────────────────────────────────────────────────────────────

def _post(api_url: str, path: str, body: dict | list, dry_run: bool) -> None:
    url = f"{api_url}{path}"
    if dry_run:
        log.info("[dry] POST %-40s  %s", path, json.dumps(body)); return
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data,
                                   headers={"Content-Type": "application/json",
                                            "X-Dispatcher": "1"},
                                   method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')}") from e


# ── Slot assignment ───────────────────────────────────────────────────────────

def _plan_hash(tou_slots: list) -> str:
    return hashlib.sha1(json.dumps(tou_slots, sort_keys=True).encode()).hexdigest()[:10]

def load_state() -> dict:
    return json.loads(_STATE_FILE.read_text()) if _STATE_FILE.exists() else {}

def save_state(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state, indent=2))

def verify_pending(pending: dict, fw: dict) -> bool:
    """Compare pending writes against firmware; log result. Returns True if all confirmed."""
    fw_storage = fw.get("storage", {})
    fw_battery = fw.get("battery") or {}
    fw_dis     = {s["slot"]: s for s in (fw.get("tou") or {}).get("discharge_slots", [])}
    ok, miss   = [], []

    if "export" in pending:
        (ok if fw_storage.get("allow_export") == pending["export"] else miss).append("export")

    if "charge_amps" in pending:
        per     = _per_bat(pending["charge_amps"])
        matched = (round(fw_battery.get("bat1_charge_current_a", -1)) == round(per)
                   and round(fw_battery.get("bat2_charge_current_a", -1)) == round(per))
        (ok if matched else miss).append("charge_amps")

    if "tou_slots" in pending:
        slot_ok = True
        for d in pending["tou_slots"]:
            f = fw_dis.get(d["slot"], {})
            if d["enabled"]:
                if not (f.get("enabled")
                        and f.get("start") == d["start"]
                        and f.get("end")   == d["end"]
                        and round(f.get("current_a", -1)) == round(d["current_a"])
                        and f.get("soc_pct") == d.get("soc_pct")):
                    slot_ok = False; break
            elif f.get("enabled"):
                slot_ok = False; break
        (ok if slot_ok else miss).append("tou_slots")

    if ok:   log.info("pending confirmed: %s", ok)
    if miss: log.warning("pending NOT applied (will retry): %s", miss)
    return not miss

def get_slot_order(tou_slots: list, state: dict) -> list[int]:
    """Return persistent random slot order for today's plan (regenerate on plan change)."""
    ph = _plan_hash(tou_slots)
    if (state.get("date") == date.today().isoformat()
            and state.get("plan_hash") == ph
            and state.get("slot_order")):
        return state["slot_order"]
    order = list(range(1, 7))
    random.shuffle(order)
    log.info("slot order: %s  (plan hash %s)", order, ph)
    return order

def build_desired_slots(tou_slots: list, slot_order: list[int]) -> list[dict]:
    """Assign plan windows to randomised inverter slot numbers; pad unused to 6."""
    mapping: dict[int, dict] = {}
    # Use the chosen slot numbers in ascending order so the inverter sees
    # windows in chronological order (wear-leveling still picks random slots).
    active_slots = sorted(slot_order[:len(tou_slots)])
    sorted_tou   = sorted(tou_slots, key=lambda s: s["start"])
    for slot_num, sl in zip(active_slots, sorted_tou):
        mapping[slot_num] = {
            "slot":      slot_num,
            "enabled":   True,
            "start":     _tou_t(sl["start"]),
            "end":       _tou_t(sl["end"]),
            "soc_pct":   sl["soc_floor_pct"],
            "current_a": sl["amps"],
        }
    for slot_num in slot_order[len(tou_slots):]:
        mapping[slot_num] = {"slot": slot_num, "enabled": False,
                              "start": "00:00", "end": "00:00"}
    return [mapping[n] for n in sorted(mapping)]


# ── TOU sync ──────────────────────────────────────────────────────────────────

def sync_tou(desired: list, tou_slots: list, fw_discharge: list,
             api_url: str, dry_run: bool) -> bool:
    fw = {s["slot"]: s for s in fw_discharge}
    changed = False
    for d in desired:
        f = fw.get(d["slot"], {})
        if d["enabled"]:
            diff = (not f.get("enabled")
                    or f.get("start") != d["start"]
                    or f.get("end")   != d["end"]
                    or round(f.get("current_a", -1)) != round(d["current_a"])
                    or f.get("soc_pct") != d.get("soc_pct"))
        else:
            diff = bool(f.get("enabled"))
        if diff:
            changed = True; break

    if not changed:
        log.debug("tou slots: up to date"); return False

    summary = [(d["slot"], d["start"] + "–" + d["end"] if d["enabled"] else "off")
               for d in desired if d["enabled"] or fw.get(d["slot"], {}).get("enabled")]
    log.info("tou slots: %s", summary)
    _post(api_url, "/api/settings/tou/discharge/all", desired, dry_run)
    return True


# ── Export ────────────────────────────────────────────────────────────────────

def apply_export(want: bool, fw_export: bool | None,
                 sell_kw: float, api_url: str, dry_run: bool) -> bool:
    if fw_export is not None and fw_export == want:
        log.debug("export: unchanged (%s)", want); return False
    body: dict = {"allow_export": want}
    if want and sell_kw > 0:
        body["max_export_power_w"] = int(sell_kw * 1000)
    log.info("export: %s → %s", fw_export, want)
    _post(api_url, "/api/settings/storage", body, dry_run)
    return True


# ── Charge amps ───────────────────────────────────────────────────────────────

def apply_charge_amps(total_a: float, fw_battery: dict | None,
                      api_url: str, dry_run: bool) -> bool:
    per = _per_bat(total_a)
    if fw_battery:
        if (round(fw_battery.get("bat1_charge_current_a", -1)) == round(per)
                and round(fw_battery.get("bat2_charge_current_a", -1)) == round(per)):
            log.debug("charge_amps: unchanged (%.0f A × 2)", per); return False
    log.info("charge_amps: %.0f A total → %.0f A each (BAT1+BAT2)", total_a, per)
    _post(api_url, "/api/settings/battery",
          {"bat1_charge_current_a": per, "bat2_charge_current_a": per}, dry_run)
    return True


# ── SoC ───────────────────────────────────────────────────────────────────────

def get_soc(prom_url: str, instance_id: str) -> int | None:
    if not prom_url: return None
    q   = (f'battery_soc_pct{{instance_id="{instance_id}"}}' if instance_id
           else "battery_soc_pct")
    url = f"{prom_url}/api/v1/query?" + urllib.parse.urlencode({"query": q})
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            res = json.loads(r.read()).get("data", {}).get("result", [])
        return int(float(res[0]["value"][1])) if res else None
    except Exception as e:
        log.warning("soc: prometheus unreachable (%s)", e); return None


def _soc_guard_would_fire(tou_slots: list, now_min: int, soc: int | None) -> bool:
    if soc is None: return False
    for sl in tou_slots:
        if _tm(sl["start"]) <= now_min < _tm(sl["end"]):
            if soc <= sl["soc_floor_pct"] + 2:
                return True
    return False

def soc_guard(tou_slots: list, fw_discharge: list, now_min: int,
              soc: int | None, api_url: str, dry_run: bool) -> bool:
    if soc is None: return False
    for sl in tou_slots:
        if _tm(sl["start"]) <= now_min < _tm(sl["end"]):
            floor = sl["soc_floor_pct"]
            if soc <= floor + 2:
                log.warning("soc guard: %d%% ≤ floor %d%% + 2 — disabling active discharge slot(s)",
                            soc, floor)
                for fw in fw_discharge:
                    if (fw.get("enabled")
                            and _tm(fw.get("start", "99:00")) <= now_min
                            < _tm(fw.get("end", "00:00"))):
                        _post(api_url, f"/api/settings/tou/discharge/{fw['slot']}",
                              {"enabled": False, "start": "00:00", "end": "00:00"}, dry_run)
                        log.info("soc guard: slot %d disabled", fw["slot"])
                return True
    return False


# ── Show map ─────────────────────────────────────────────────────────────────

_CIRCLE = ["①","②","③","④","⑤","⑥"]
_RST  = "\033[0m"
_BOLD = "\033[1m"
_DIM  = "\033[2m"
_GRN  = "\033[32m"
_YLW  = "\033[33m"
_BLU  = "\033[34m"
_CYN  = "\033[36m"
_RED  = "\033[31m"

def show_map(m: dict, now_min: int) -> None:
    gen  = m.get("generated_at", "")
    print(f"\n{_BOLD}Map:{_RST}  {m.get('date','')}  {m.get('instance_id','')}  "
          f"[{m.get('algo','')}]  generated {gen[11:19] if len(gen) > 10 else gen}")

    tou = m.get("tou_slots", [])
    print(f"\n{_BOLD}TOU Discharge Slots:{_RST}")
    if tou:
        for i, sl in enumerate(tou):
            print(f"  {_CYN}{_CIRCLE[i]}{_RST}  {sl['start']} – {sl['end']}"
                  f"   {_YLW}{sl['amps']} A{_RST}  floor {sl['soc_floor_pct']}%")
    else:
        print(f"  {_DIM}(none){_RST}")

    events = m.get("events", [])
    want   = desired_state(events, now_min)
    # Find which event is "active" (last event at or before now)
    active_time = None
    for ev in events:
        if _tm(ev["time"]) <= now_min + EVENT_TOLERANCE_MIN:
            active_time = ev["time"]

    print(f"\n{_BOLD}Events:{_RST}")
    for ev in events:
        t       = ev["time"]
        is_now  = (t == active_time)
        parts   = []

        if "export" in ev:
            if ev["export"]:
                parts.append(f"{_GRN}export ON{_RST}")
            else:
                parts.append(f"{_RED}export OFF{_RST}")

        if "charge_amps" in ev:
            parts.append(f"charge → {_YLW}{ev['charge_amps']} A{_RST}")

        if "soc_floor_pct" in ev:
            parts.append(f"floor {ev['soc_floor_pct']}%")

        marker = f" {_BOLD}◀ now{_RST}" if is_now else ""
        line   = f"  {_BOLD if is_now else ''}{t}{_RST if is_now else ''}   {'  '.join(parts)}{marker}"
        print(line)

    print(f"\n{_DIM}now = {_fmt(now_min)}   "
          f"desired: export={want.get('export','?')}  "
          f"charge_amps={want.get('charge_amps','?')}{_RST}\n")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Solar plan dispatcher")
    parser.add_argument("--config",   default=str(_DEFAULT_CFG))
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--time",     default=None, help="Override current time HH:MM")
    parser.add_argument("--instance", default=None, help="instance_id to load")
    parser.add_argument("--show",     action="store_true", help="Print map and exit")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    m   = load_map(args.instance)

    _now    = datetime.now()
    now_min = _tm(args.time) if args.time else _now.hour * 60 + _now.minute

    if args.show:
        show_map(m, now_min); return

    log.info("run at %s", _fmt(now_min))

    tou_slots   = m.get("tou_slots", [])
    events      = m.get("events", [])
    sell_kw     = float(m.get("sell_kw", 0))
    instance_id = m.get("instance_id", "")

    # ── Desired state from past events ────────────────────────────────────────
    want = desired_state(events, now_min)
    if want:
        log.info("desired: %s", want)

    # ── SoC (always read — cheap Prometheus query, needed for guard pre-check) ──
    soc = get_soc(cfg["prom_url"], instance_id)
    if soc is not None:
        log.info("soc: %d%%", soc)

    # ── Early exit if nothing has changed since last run ──────────────────────
    state       = load_state()
    plan_hash   = _plan_hash(tou_slots)
    fingerprint = f"{plan_hash}|{json.dumps(want, sort_keys=True)}"
    if (not state.get("pending")
            and state.get("fingerprint") == fingerprint
            and not _soc_guard_would_fire(tou_slots, now_min, soc)):
        log.debug("desired state unchanged — skipping firmware read")
        return

    # ── Firmware state ────────────────────────────────────────────────────────
    try:
        fw = read_fw(cfg["api_url"])
    except Exception as e:
        log.error("cannot read firmware: %s", e); sys.exit(1)

    fw_storage   = fw.get("storage", {})
    fw_discharge = (fw.get("tou") or {}).get("discharge_slots", [])
    fw_battery   = fw.get("battery")

    # ── Verify pending writes from last run ───────────────────────────────────
    pending = state.get("pending")
    if pending:
        all_confirmed = verify_pending(pending, fw)
        if all_confirmed:
            state["pending"] = None

    # ── SoC guard ─────────────────────────────────────────────────────────────
    soc_hit = soc_guard(tou_slots, fw_discharge, now_min, soc, cfg["api_url"], args.dry_run)

    # ── TOU slot sync ─────────────────────────────────────────────────────────
    slot_order = get_slot_order(tou_slots, state)
    desired    = build_desired_slots(tou_slots, slot_order)
    tou_hit    = sync_tou(desired, tou_slots, fw_discharge, cfg["api_url"], args.dry_run)

    # ── Export ────────────────────────────────────────────────────────────────
    exp_hit = False
    if "export" in want:
        exp_hit = apply_export(want["export"], fw_storage.get("allow_export"),
                               sell_kw, cfg["api_url"], args.dry_run)

    # ── Charge amps ───────────────────────────────────────────────────────────
    chg_hit = False
    if "charge_amps" in want:
        chg_hit = apply_charge_amps(want["charge_amps"], fw_battery,
                                    cfg["api_url"], args.dry_run)

    if not any([soc_hit, tou_hit, exp_hit, chg_hit]):
        log.info("all up to date")

    # ── Save state (including pending for next-run confirmation) ──────────────
    if not args.dry_run:
        new_pending: dict = {}
        if tou_hit: new_pending["tou_slots"]   = desired
        if exp_hit: new_pending["export"]      = want["export"]
        if chg_hit: new_pending["charge_amps"] = want["charge_amps"]
        save_state({
            "date":        date.today().isoformat(),
            "plan_hash":   plan_hash,
            "slot_order":  slot_order,
            "fingerprint": fingerprint,
            "pending":     new_pending or None,
        })


if __name__ == "__main__":
    main()
