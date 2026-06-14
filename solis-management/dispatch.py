#!/usr/bin/env python3
"""
dispatch.py — 5-minute cron dispatcher for solar plan execution.

Reads the latest dispatch map (written by solar-planner) and configures the
inverter via the local solis-api.  State is tracked per TOU slot so only
completed slots are reconfigured; active slots are left untouched.

TOU discharge slots used: 5 and 6 (sell_batt windows, in order).

Behaviour per run:
  • Determine which TOU slots are still active (window not yet ended)
  • Assign the next uncovered sell_batt windows to the free (completed) slots
  • Skip if nothing changed
  • On HTTP failure → retry once; if still failing, exit without saving state
    (next cron run retries automatically)

Crontab entry (every 5 min):
  */5 * * * * /usr/bin/python3 /path/to/dispatch.py >> /var/log/solar_dispatch.log 2>&1

Config (solis-monitor/config.cfg):
  [SolisAPI]
  host = 0.0.0.0
  port = 5000
  mothership_prometheus_api = http://10.100.0.1/
"""

import argparse
import configparser
import json
import logging
import sys
import urllib.request
import urllib.error
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


_MAPS_DIR    = Path(__file__).resolve().parent / "maps"
_STATE_FILE  = Path(__file__).resolve().parent / "dispatch_state.json"
_LOG_FILE    = Path(__file__).resolve().parent / "dispatch.log"
_DEFAULT_CFG = Path(__file__).resolve().parent.parent / "solis-monitor" / "config.cfg"


# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("dispatch")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(_LOG_FILE, maxBytes=1_000_000, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log = _setup_logging()

_TOU_SLOTS = [5, 6]   # TOU discharge slots used for sell_batt windows


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(str(path))
    srv  = cfg["SolisAPI"] if "SolisAPI" in cfg else {}
    port = int(srv.get("port", 5000))
    return {
        "management_url": f"http://localhost:{port}",
        "prom_url":       srv.get("mothership_prometheus_api", "").rstrip("/"),
    }


# ── Map loading ───────────────────────────────────────────────────────────────

def load_map() -> dict:
    today      = date.today().isoformat()
    candidates = sorted(_MAPS_DIR.glob("map_*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        log.error("no dispatch map found in %s", _MAPS_DIR)
        sys.exit(1)

    for p in candidates:
        data = json.loads(p.read_text())
        if data.get("date") == today:
            log.debug("loaded map %s", p.name)
            return data

    path     = candidates[0]
    map_data = json.loads(path.read_text())
    log.warning("no map for today (%s), using %s (date: %s)",
                today, path.name, map_data.get("date", "?"))
    return map_data


def _tm(t: str) -> int:
    return int(t[:2]) * 60 + int(t[3:])


def _tou_time(t: str) -> str:
    """Normalize plan time for inverter API — '24:00' is invalid, use '00:00'."""
    return "00:00" if t == "24:00" else t


def _current_segment(dispatch_map: dict, now_minutes: int) -> dict | None:
    """Return the dispatch segment whose window contains now_minutes, or None."""
    for seg in dispatch_map.get("segments", []):
        if _tm(seg["start"]) <= now_minutes < _tm(seg["end"]):
            return seg
    return None


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if _STATE_FILE.exists():
        return json.loads(_STATE_FILE.read_text())
    return {}


def save_state(map_date: str, slots: dict, allow_export: bool = False,
               soc_disabled: list | None = None) -> None:
    _STATE_FILE.write_text(json.dumps({
        "date":         map_date,
        "applied_at":   datetime.now().isoformat(timespec="seconds"),
        "allow_export": allow_export,
        "soc_disabled": soc_disabled or [],
        "slots":        slots,
    }))


# ── Slot tracking ─────────────────────────────────────────────────────────────

def _compute_updates(dispatch_map: dict, state_slots: dict, now_minutes: int,
                     soc_disabled: list | None = None) -> dict:
    """
    Returns {slot_num: seg_or_None} for slots that need reconfiguring.
    Active slots (window not yet ended) are skipped entirely.
    Segments whose start time is in soc_disabled are excluded from assignment.
    """
    soc_disabled = soc_disabled or []
    all_sell = [s for s in dispatch_map.get("segments", [])
                if s["action"] == "sell_batt"]

    # Slots still actively running (window hasn't ended yet)
    active: dict[int, dict] = {}
    for slot_str, seg in state_slots.items():
        if seg and _tm(seg["end"]) > now_minutes:
            active[int(slot_str)] = seg

    # Future sell windows not yet covered by any active slot.
    # "Covered" means overlapping (not just exact match) — a regenerated plan may shift
    # the window boundary slightly; an overlapping active slot already handles that period.
    # Segments disabled due to SoC floor are also excluded.
    active_segs = list(active.values())
    available = [
        s for s in all_sell
        if _tm(s["end"]) > now_minutes
        and s["start"] not in soc_disabled
        and not any(
            _tm(s["start"]) < _tm(a["end"]) and _tm(a["start"]) < _tm(s["end"])
            for a in active_segs
        )
    ]

    # Free slots (completed or never used) get new assignments
    free_slots = sorted(s for s in _TOU_SLOTS if s not in active)
    updates = {}
    for i, slot_num in enumerate(free_slots):
        new_seg = available[i] if i < len(available) else None
        cur_seg = state_slots.get(str(slot_num))
        cur_key = (cur_seg["start"], cur_seg["end"]) if cur_seg else None
        new_key = (new_seg["start"], new_seg["end"]) if new_seg else None
        if new_key != cur_key:
            updates[slot_num] = new_seg
    return updates


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post(url: str, body: dict, dry_run: bool) -> dict:
    if dry_run:
        log.info("[DRY RUN] POST %s  %s", url, json.dumps(body))
        return {"ok": True}
    data = json.dumps(body).encode()
    req  = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
            log.debug("POST %s → %s", url, resp)
            return resp
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {body_text}") from e


def _post_with_retry(url: str, body: dict, dry_run: bool) -> dict:
    try:
        return _post(url, body, dry_run)
    except Exception as e:
        log.warning("%s — retrying once…", e)
        return _post(url, body, dry_run)   # raises on second failure


# ── SoC reading ───────────────────────────────────────────────────────────────

def _get_soc(prom_url: str) -> int | None:
    """Return live battery SoC % from Prometheus instant query (~100ms). None on failure."""
    import urllib.parse
    query = urllib.parse.urlencode({"query": "battery_soc_pct"})
    url   = f"{prom_url}/api/v1/query?{query}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        result = data.get("data", {}).get("result", [])
        if result:
            soc = int(float(result[0]["value"][1]))
            log.debug("live SoC from Prometheus: %d%%", soc)
            return soc
        log.warning("Prometheus returned no result for battery_soc_pct")
        return None
    except Exception as e:
        log.warning("could not read SoC from Prometheus (%s): %s", url, e)
        return None


# ── Apply ─────────────────────────────────────────────────────────────────────

def apply_plan(mgmt_url: str, updates: dict, any_sell: bool,
               max_export_w: int, dry_run: bool) -> None:
    storage_body: dict = {"allow_export": any_sell}
    if any_sell and max_export_w > 0:
        storage_body["max_export_power_w"] = max_export_w
    _post_with_retry(f"{mgmt_url}/api/settings/storage", storage_body, dry_run)
    for slot_num, seg in sorted(updates.items()):
        if seg is None:
            log.info("  slot %d: disabled", slot_num)
            _post_with_retry(f"{mgmt_url}/api/settings/tou/discharge/{slot_num}",
                             {"enabled": False}, dry_run)
        else:
            log.info("  slot %d: %s–%s  floor=%d%%",
                     slot_num, seg["start"], seg["end"], seg.get("soc_floor_pct", 15))
            _post_with_retry(f"{mgmt_url}/api/settings/tou/discharge/{slot_num}", {
                "enabled": True,
                "start":   _tou_time(seg["start"]),
                "end":     _tou_time(seg["end"]),
                "soc_pct": seg.get("soc_floor_pct", 15),
            }, dry_run)
    log.info("apply: done (%d slot(s) updated)", len(updates))


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Solar dispatch cron — apply plan to inverter")
    parser.add_argument("--config",  default=str(_DEFAULT_CFG), help="Path to config.cfg")
    parser.add_argument("--dry-run", action="store_true",        help="Print actions, no writes")
    parser.add_argument("--time",    default=None,               help="Override current time HH:MM (testing)")
    args = parser.parse_args()

    cfg          = load_config(Path(args.config))
    dispatch_map = load_map()
    map_date     = dispatch_map.get("date", "?")
    today        = date.today().isoformat()

    if map_date != today:
        log.warning("map date %s != today %s", map_date, today)

    if args.time:
        h, m = map(int, args.time.split(":"))
        now_minutes = h * 60 + m
    else:
        _now = datetime.now()
        now_minutes = _now.hour * 60 + _now.minute

    state       = load_state()
    state_today = state.get("date") == today
    state_slots = (state.get("slots") or {str(s): None for s in _TOU_SLOTS}
                   if state_today
                   else {str(s): None for s in _TOU_SLOTS})
    soc_disabled: list = (state.get("soc_disabled") or []) if state_today else []

    # ── allow_export: track independently of slot changes ─────────────────────
    # Desired state comes from the current time's segment in the dispatch map.
    # Hold / charge periods → False; sell_batt / sell_solar periods → True.
    cur_seg        = _current_segment(dispatch_map, now_minutes)
    desired_export = cur_seg is not None and cur_seg["action"] in ("sell_batt", "sell_solar")
    # None means "unknown / first run today" — treat as changed so we always
    # apply the correct state on the first run after midnight or a restart.
    last_export    = state.get("allow_export") if state_today else None
    export_changed = desired_export != last_export

    # ── SoC floor guard ───────────────────────────────────────────────────────
    # During sell_batt windows, read live SoC from Prometheus every 5 min.
    # If SoC has reached the planned floor, proactively disable the TOU slot
    # before the firmware floor triggers (which would cause grid import).
    soc_triggered = False
    if (cfg.get("prom_url") and cur_seg and cur_seg["action"] == "sell_batt"
            and cur_seg["start"] not in soc_disabled):
        live_soc = _get_soc(cfg["prom_url"])
        if live_soc is not None and live_soc <= cur_seg["soc_floor_pct"]:
            log.warning(
                "SoC %d%% ≤ floor %d%% for segment %s–%s — disabling TOU slots",
                live_soc, cur_seg["soc_floor_pct"],
                cur_seg["start"], cur_seg["end"],
            )
            for slot_str, seg in list(state_slots.items()):
                if seg and _tm(seg["start"]) <= now_minutes < _tm(seg["end"]):
                    try:
                        _post_with_retry(
                            f"{cfg['management_url']}/api/settings/tou/discharge/{slot_str}",
                            {"enabled": False}, args.dry_run,
                        )
                        log.info("  slot %s disabled (SoC floor)", slot_str)
                        state_slots[slot_str] = None
                    except Exception as e:
                        log.error("could not disable slot %s: %s", slot_str, e)
            soc_disabled.append(cur_seg["start"])
            desired_export = False
            export_changed = desired_export != last_export
            soc_triggered  = True

    # ── TOU slot assignments ───────────────────────────────────────────────────
    updates = _compute_updates(dispatch_map, state_slots, now_minutes, soc_disabled)

    if not updates and not export_changed and not soc_triggered:
        log.info("all slots up to date — skipping")
        return

    new_slots = {**state_slots, **{str(k): v for k, v in updates.items()}}

    # Max export power: prefer the currently-active segment's value; fall back
    # to any configured future slot (for when we're in a hold period but have
    # sell slots already programmed).
    if desired_export and cur_seg and cur_seg.get("sell_kw"):
        sell_kw = cur_seg["sell_kw"]
    else:
        sell_kw = next((seg["sell_kw"] for seg in new_slots.values() if seg), 0.0)
    max_export_w = int(round(sell_kw * 1000 / 100) * 100)

    if export_changed:
        action_str = cur_seg["action"] if cur_seg else "hold"
        log.info("allow_export: %s → %s  (current segment: %s)",
                 last_export, desired_export, action_str)
    if updates:
        log.info("updating %d slot(s): %s", len(updates),
                 ", ".join(
                     f"slot{k}={'off' if v is None else v['start'] + '–' + v['end']}"
                     for k, v in sorted(updates.items())
                 ))
    if desired_export:
        log.info("max_export_power_w = %d", max_export_w)

    try:
        apply_plan(cfg["management_url"], updates, desired_export, max_export_w,
                   dry_run=args.dry_run)
    except Exception as e:
        log.error("%s — state NOT saved, will retry next run", e)
        sys.exit(1)

    if not args.dry_run:
        save_state(today, new_slots, allow_export=desired_export, soc_disabled=soc_disabled)
        log.info("state saved  slots=%s  allow_export=%s  soc_disabled=%s",
                 new_slots, desired_export, soc_disabled)


if __name__ == "__main__":
    main()
