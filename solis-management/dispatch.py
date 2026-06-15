#!/usr/bin/env python3
"""
dispatch.py — 5-minute cron dispatcher for solar plan execution.

Reads the latest dispatch map (written by solar-planner) and configures the
inverter via the local solis-api.  Firmware state is read on every run via
GET /api/settings — no stale state_slots, no window conflicts.

TOU discharge slots used: all 6.

Behaviour per run:
  • Read actual firmware TOU state (all 6 discharge slots + allow_export)
  • Determine which slots are actively running (enabled, window not yet ended)
  • Assign the next uncovered sell_batt windows to free slots
  • Compare desired vs firmware — update only slots that differ
  • Disables are sent first (with zeroed times) to prevent overlap conflicts
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

_TOU_SLOTS = [1, 2, 3, 4, 5, 6]   # all TOU discharge slots


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


# ── State (soc_disabled only — slot state read live from firmware) ─────────────

def load_state() -> dict:
    if _STATE_FILE.exists():
        return json.loads(_STATE_FILE.read_text())
    return {}


def save_state(map_date: str, soc_disabled: list | None = None) -> None:
    _STATE_FILE.write_text(json.dumps({
        "date":         map_date,
        "saved_at":     datetime.now().isoformat(timespec="seconds"),
        "soc_disabled": soc_disabled or [],
    }))


# ── Firmware state ────────────────────────────────────────────────────────────

def _read_fw_state(mgmt_url: str) -> tuple[list, bool | None]:
    """
    Read live TOU discharge slot state and allow_export from the inverter.
    Returns (fw_slots, fw_allow_export).
    fw_slots is a list of dicts: {slot, enabled, start, end, soc_pct, ...}
    Returns ([], None) on failure — caller treats None as "unknown, force update".
    """
    url = f"{mgmt_url}/api/settings"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        tou       = data.get("tou") or {}
        fw_slots  = tou.get("discharge_slots", [])
        fw_export = (data.get("storage") or {}).get("allow_export")
        log.debug("firmware: %d discharge slots read, allow_export=%s", len(fw_slots), fw_export)
        for fw in fw_slots:
            log.debug("  fw slot %s: enabled=%s  %s–%s  soc=%s",
                      fw.get("slot"), fw.get("enabled"),
                      fw.get("start"), fw.get("end"), fw.get("soc_pct"))
        return fw_slots, fw_export
    except Exception as e:
        log.warning("could not read firmware state: %s", e)
        return [], None


# ── Slot assignment ───────────────────────────────────────────────────────────

def _compute_updates(dispatch_map: dict, fw_slots: list, now_minutes: int,
                     soc_disabled: list | None = None) -> dict:
    """
    Returns {slot_num: seg_or_None} for slots whose firmware state differs from desired.
    Active firmware slots (enabled, window not yet ended) are left untouched.
    Segments whose start time is in soc_disabled are excluded from assignment.
    """
    soc_disabled = soc_disabled or []
    all_sell = [s for s in dispatch_map.get("segments", [])
                if s["action"] == "sell_batt"]

    # Active firmware slots: enabled AND window currently running (start ≤ now < end).
    # Slots that are enabled but whose window hasn't started yet are safe to reassign.
    active_fw: dict[int, dict] = {
        fw["slot"]: fw for fw in fw_slots
        if (fw.get("enabled")
            and _tm(fw.get("start", "00:00")) <= now_minutes
            < _tm(fw.get("end", "00:00")))
    }

    # Future sell segments not yet covered by an active fw slot and not SoC-disabled
    active_segs = list(active_fw.values())
    available = [
        s for s in all_sell
        if _tm(s["end"]) > now_minutes
        and s["start"] not in soc_disabled
        and not any(
            _tm(s["start"]) < _tm(a["end"]) and _tm(a["start"]) < _tm(s["end"])
            for a in active_segs
        )
    ]

    # Free slots (disabled or window already ended)
    free_slots = sorted(fw["slot"] for fw in fw_slots if fw["slot"] not in active_fw)

    updates: dict[int, dict | None] = {}
    for i, slot_num in enumerate(free_slots):
        new_seg = available[i] if i < len(available) else None
        cur_fw  = next((fw for fw in fw_slots if fw["slot"] == slot_num), {})

        # Build comparable keys
        if new_seg:
            desired = (True,  _tou_time(new_seg["start"]), _tou_time(new_seg["end"]),
                       new_seg.get("soc_floor_pct", 15))
        else:
            desired = (False, "00:00", "00:00", None)

        fw_enabled = bool(cur_fw.get("enabled"))
        current = (fw_enabled,
                   cur_fw.get("start", "00:00"),
                   cur_fw.get("end",   "00:00"),
                   cur_fw.get("soc_pct") if fw_enabled else None)

        log.debug("  slot %d: desired=%s  current=%s  diff=%s",
                  slot_num, desired, current, desired != current)
        if desired != current:
            updates[slot_num] = new_seg

    log.debug("compute_updates → %s", {k: (v["start"] + "–" + v["end"] if v else "off")
                                        for k, v in updates.items()})
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

def _get_soc(prom_url: str, instance_id: str = "") -> int | None:
    """Return live battery SoC % from Prometheus instant query (~100ms). None on failure."""
    import urllib.parse
    prom_query = "battery_soc_pct"
    if instance_id:
        prom_query = f'battery_soc_pct{{instance_id="{instance_id}"}}'
    url = f"{prom_url}/api/v1/query?" + urllib.parse.urlencode({"query": prom_query})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        result = data.get("data", {}).get("result", [])
        if result:
            soc = int(float(result[0]["value"][1]))
            log.debug("live SoC from Prometheus (%s): %d%%", instance_id or "any", soc)
            return soc
        log.warning("Prometheus returned no result for %s", prom_query)
        return None
    except Exception as e:
        log.info("SoC guard skipped — Prometheus unreachable (%s): %s", url, e)
        return None


# ── Apply ─────────────────────────────────────────────────────────────────────

def apply_plan(mgmt_url: str, updates: dict, any_sell: bool,
               max_export_w: int, dry_run: bool) -> None:
    storage_body: dict = {"allow_export": any_sell}
    if any_sell and max_export_w > 0:
        storage_body["max_export_power_w"] = max_export_w
    _post_with_retry(f"{mgmt_url}/api/settings/storage", storage_body, dry_run)

    if not updates:
        return

    # Build batch payload for /api/settings/tou/discharge/all
    # One atomic write: all slot register data in one FC16, bitmask in one RMW → no overlap conflicts
    batch: list[dict] = []
    for slot_num, seg in sorted(updates.items()):
        if seg is None:
            log.info("  slot %d: disabled", slot_num)
            batch.append({"slot": slot_num, "enabled": False,
                          "start": "00:00", "end": "00:00"})
        else:
            log.info("  slot %d: %s–%s  floor=%d%%",
                     slot_num, seg["start"], seg["end"], seg.get("soc_floor_pct", 15))
            batch.append({"slot": slot_num, "enabled": True,
                          "start":   _tou_time(seg["start"]),
                          "end":     _tou_time(seg["end"]),
                          "soc_pct": seg.get("soc_floor_pct", 15)})

    _post_with_retry(f"{mgmt_url}/api/settings/tou/discharge/all", batch, dry_run)
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
    instance_id  = dispatch_map.get("instance_id", "")
    today        = date.today().isoformat()

    if map_date != today:
        log.warning("map date %s != today %s", map_date, today)

    if args.time:
        h, m = map(int, args.time.split(":"))
        now_minutes = h * 60 + m
    else:
        _now = datetime.now()
        now_minutes = _now.hour * 60 + _now.minute

    # ── Read firmware state (source of truth for slots + allow_export) ─────────
    fw_slots, fw_allow_export = _read_fw_state(cfg["management_url"])

    # ── Load soc_disabled from state file (the only thing we persist) ──────────
    state        = load_state()
    state_today  = state.get("date") == today
    soc_disabled: list = (state.get("soc_disabled") or []) if state_today else []

    # ── allow_export: desired vs firmware ─────────────────────────────────────
    cur_seg        = _current_segment(dispatch_map, now_minutes)
    desired_export = cur_seg is not None and cur_seg["action"] in ("sell_batt", "sell_solar")

    # If plan was regenerated and shifted the sell window, but an fw slot is still
    # actively running, keep allow_export True — the inverter is still selling.
    if not desired_export:
        for fw in fw_slots:
            if (fw.get("enabled")
                    and _tm(fw.get("start", "00:00")) <= now_minutes
                    < _tm(fw.get("end", "00:00"))):
                desired_export = True
                break

    # fw_allow_export=None means we couldn't read firmware — treat as changed
    export_changed = (fw_allow_export is None) or (desired_export != fw_allow_export)

    # ── SoC floor guard ───────────────────────────────────────────────────────
    soc_triggered = False
    if (cfg.get("prom_url") and cur_seg and cur_seg["action"] == "sell_batt"
            and cur_seg["start"] not in soc_disabled):
        live_soc = _get_soc(cfg["prom_url"], instance_id)
        if live_soc is not None and live_soc <= cur_seg["soc_floor_pct"]:
            log.warning(
                "SoC %d%% ≤ floor %d%% for segment %s–%s — disabling active TOU slot(s)",
                live_soc, cur_seg["soc_floor_pct"],
                cur_seg["start"], cur_seg["end"],
            )
            for fw in fw_slots:
                if (fw.get("enabled")
                        and _tm(fw.get("start", "00:00")) <= now_minutes
                        < _tm(fw.get("end", "00:00"))):
                    slot_num = fw["slot"]
                    try:
                        _post_with_retry(
                            f"{cfg['management_url']}/api/settings/tou/discharge/{slot_num}",
                            {"enabled": False, "start": "00:00", "end": "00:00"},
                            args.dry_run,
                        )
                        log.info("  slot %d disabled (SoC floor)", slot_num)
                        # Patch fw_slots so _compute_updates sees this slot as free
                        fw["enabled"] = False
                        fw["start"]   = "00:00"
                        fw["end"]     = "00:00"
                    except Exception as e:
                        log.error("could not disable slot %d: %s", slot_num, e)
            soc_disabled.append(cur_seg["start"])
            desired_export = False
            export_changed = True
            soc_triggered  = True

    # ── TOU slot assignments ───────────────────────────────────────────────────
    updates = _compute_updates(dispatch_map, fw_slots, now_minutes, soc_disabled)

    if not updates and not export_changed and not soc_triggered:
        log.info("all slots up to date — skipping")
        return

    # Max export power: from current segment or first upcoming sell segment
    if desired_export and cur_seg and cur_seg.get("sell_kw"):
        sell_kw = cur_seg["sell_kw"]
    else:
        upcoming = [s for s in dispatch_map.get("segments", [])
                    if s["action"] == "sell_batt" and _tm(s["end"]) > now_minutes]
        sell_kw = upcoming[0]["sell_kw"] if upcoming else 0.0
    max_export_w = int(round(sell_kw * 1000 / 100) * 100)

    if export_changed:
        action_str = cur_seg["action"] if cur_seg else "hold"
        log.info("allow_export: %s → %s  (current segment: %s)",
                 fw_allow_export, desired_export, action_str)
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
        save_state(today, soc_disabled=soc_disabled)
        log.info("state saved  soc_disabled=%s", soc_disabled)


if __name__ == "__main__":
    main()
