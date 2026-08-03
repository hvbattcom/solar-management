#!/usr/bin/env python3
"""
dispatcher.py — Solar plan event dispatcher for Deye (5-min cron).

Same job as solis/dispatcher.py — turn the planner's dispatch map into inverter
settings — but Deye's Time-of-Use model is shaped differently, and that shape
drives the whole design:

  Solis  6 discharge WINDOWS, each with its own start, end and enable bit.
         Time not covered by any enabled window is plain self-use.

  Deye   6 time POINTS. Slot N runs from its own start time until slot N+1's
         (slot 6 wraps around midnight to slot 1), so the six slots always
         tile the full 24 h and there is no per-slot enable — only a global
         schedule switch (register 146). "Idle" is not the absence of a slot;
         it is a slot configured to do nothing interesting.

Three consequences:

  1. A discharge window costs TWO boundaries (turn selling on, turn it back
     off), so 6 registers hold at most ~3 windows against Solis's 6.  Overflow
     is handled like Solis: drop windows already in the past, then defer the
     latest ones — they slide in on a later run as earlier windows end.

  2. A slot's SOC is a TARGET the inverter actively drives the battery toward,
     not a floor it merely refuses to cross.  A target below the current level
     is an instruction to empty the pack, so every slot carries a deliberate
     value: the plan's floor inside a selling window, the soft maximum wherever
     the plan may export but wants only surplus solar sold, and the battery
     minimum otherwise, where it is free to carry the house.

  3. Export is switched with the WORK MODE, and only ever inside a planned
     window unless the plant opts out.  "Selling First" is a command to export
     up to max_sell_power sourcing from PV *and the battery* — not a permission
     the way Solis's allow_export bit is — so it is safe only while every slot
     it spans targets at or above where the battery already sits.  Since Deye
     cannot say "sell solar but not the battery", the default is to not export
     outside the plan's own discharge windows.

Both of the last two are the hard way round: an earlier build mirrored the Solis
model, and at 95% SoC with an idle slot targeting 15% it dumped a customer's
battery to grid at 26.6 kW.  Regulating max_sell_power instead of the mode was
tried as the fix and does not work — 0 W there does not hold on a cloud-linked
datalogger, and the plant's limit silently reappears.

The planner never charges from the grid, so grid_charge is written False on
every slot — which is also what stops the firmware reading the SOC field as a
charge target instead.

All amps values are split equally across the configured batteries (min 1 A each).

Crontab:  */5 * * * *  /usr/bin/python3 /path/to/dispatcher.py

No redirection needed — output goes to dispatcher.log (rotated), and the
terminal echo is suppressed when stdout is not a TTY, so cron stays quiet.
An instance only has to be named with --instance where one host serves several
plants; otherwise today's map is found on its own.
"""

import argparse
import configparser
import hashlib
import json
import logging
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path


_MAPS_DIR    = Path(__file__).resolve().parent / "maps"
_STATE_FILE  = Path(__file__).resolve().parent / "dispatcher_state.json"
_LOG_FILE    = Path(__file__).resolve().parent / "dispatcher.log"
_DEFAULT_CFG = Path(__file__).resolve().parent / "config.cfg"

EVENT_TOLERANCE_MIN = 3
TOU_SLOT_COUNT      = 6
DAY_MIN             = 1440

# The export switch is the WORK MODE. Measured on hardware: "Zero Export to CT"
# stops all export including the battery, and "Selling First" resumes it within
# seconds — both reproducibly.
#
# `max_sell_power_w` is NOT usable as the switch. The register accepts 0 and
# holds it while the mode is "Zero Export to CT", but once the mode is "Selling
# First" the plant's configured limit comes back on its own within a few minutes
# and export resumes at full power, with no local write to explain it. Measured:
# 0 W written, mode set to Selling First, and the plant was exporting 26.6 kW
# again shortly after with the register reading its old 27000. So it is kept as
# a magnitude only — the ceiling while selling — never as the on/off control.
#
# What made the first build dump a battery was not choosing the work mode; it
# was pairing "Selling First" with a slot whose SOC target sat far below the
# current level, which reads as "empty the pack". The mode is safe precisely
# when every slot it is on over targets at or above where the battery already is.
EXPORT_ON_MODE  = "Selling First"
EXPORT_OFF_MODE = "Zero Export to CT"

# The firmware stores TOU times on a 5-minute grid: write 07:07 and it reads
# back 07:05. Anything written off-grid never verifies, so the dispatcher would
# rewrite the whole block on every run — exactly the EEPROM churn to avoid.
TOU_TIME_GRID_MIN = 5


# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_log() -> logging.Logger:
    logger = logging.getLogger("deye-dispatcher")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh  = RotatingFileHandler(_LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    # Echo to the terminal only when there is one. Under cron every line would
    # otherwise have to be redirected somewhere, and redirecting to a file just
    # duplicates dispatcher.log into a second copy that nothing rotates.
    if sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger

log = _setup_log()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tm(t: str) -> int:
    if t == "24:00": return DAY_MIN
    h, m = t.split(":"); return int(h) * 60 + int(m)

def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"

def _snap(minutes: int) -> int:
    """Round a minute-of-day onto the firmware's TOU time grid."""
    g = TOU_TIME_GRID_MIN
    return ((minutes + g // 2) // g) * g


def _wall(minutes: int) -> str:
    """Wrap any minute-of-plan (the horizon runs up to 1 h past its own midnight)
    onto real wall-clock HH:MM — the firmware has no notion of a day boundary."""
    return _fmt(minutes % DAY_MIN)


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(str(path))
    srv = cfg["DeyeAPI"]      if "DeyeAPI"      in cfg else {}
    inv = cfg["DeyeInverter"] if "DeyeInverter" in cfg else {}
    raw_count = str(srv.get("battery_count", "auto")).strip().lower()
    return {
        "api_url":         f"http://localhost:{srv.get('port', 5000)}",
        "prom_url":        srv.get("mothership_prometheus_api", "").rstrip("/"),
        "battery_count":   None if raw_count in ("", "auto") else max(1, int(raw_count)),
        "max_charge_amps": int(srv.get("max_charge_amps", 0)),
        "max_sell_power_w": int(srv.get("max_sell_power_w", 0)),
        "inverter_power_w": int(float(inv.get("inverter_power_kw", 30)) * 1000),
        "idle_floor_pct":  int(srv.get("idle_floor_pct", 10)),
        "soc_max_pct":     int(srv.get("soc_max_pct", 100)),
        "sell_solar_outside_windows":
            srv.get("sell_solar_outside_windows", "false").strip().lower() == "true",
        "write_sell_bit":  srv.get("write_sell_bit", "true").strip().lower() == "true",
    }


def resolve_battery_count(opts: dict, fw_battery: dict | None) -> int:
    """How many batteries the plan's charge_amps figure is split across.

    Defaults to asking the inverter: a second battery that is not installed
    reports 0 A limits. Guessing wrong is not cosmetic — assuming two batteries
    on a single-battery plant halves the charge current for the whole day."""
    if opts["battery_count"]:
        return opts["battery_count"]
    if fw_battery and fw_battery.get("bat2_max_charge_a"):
        return 2
    return 1


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


def soc_envelope(m: dict, opts: dict) -> tuple[int, int]:
    """The battery's operating range, straight from the planner where possible.

    Deye slots hold a TARGET the inverter drives the battery toward, so every
    slot must carry a value inside this range — there is no "leave it alone".
    Newer maps publish the range explicitly; older ones only imply the low end
    through their non-selling segments."""
    lo = m.get("soc_min_pct")
    if lo is None:
        floors = [s["soc_floor_pct"] for s in m.get("segments", [])
                  if s.get("action") != "sell_batt" and "soc_floor_pct" in s]
        lo = min(floors) if floors else opts["idle_floor_pct"]
    hi = m.get("soc_max_pct") or opts["soc_max_pct"]
    lo, hi = int(lo), int(hi)
    if lo > hi:
        log.warning("soc envelope inverted (%d-%d) — using %d-%d", lo, hi, hi, lo)
        lo, hi = hi, lo
    return max(0, lo), min(100, hi)


def _clamp_soc(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def export_mask(events: list) -> list[bool]:
    """Minute-by-minute map of when the plan wants to export, over one cycle.

    The schedule repeats daily, so the state at 00:00 is whatever the last
    event of the day left behind rather than a fixed default."""
    mask  = [False] * DAY_MIN
    marks = sorted((_tm(e["time"]) % DAY_MIN, bool(e["export"]))
                   for e in events if "export" in e)
    if not marks:
        return mask
    state, idx = marks[-1][1], 0
    for minute in range(DAY_MIN):
        while idx < len(marks) and marks[idx][0] == minute:
            state = marks[idx][1]
            idx += 1
        mask[minute] = state
    return mask


def _exports_during(mask: list[bool], start: int, end: int) -> bool:
    span = range(start, end) if start < end else [*range(start, DAY_MIN), *range(0, end)]
    return any(mask[m] for m in span)


# ── Firmware read ─────────────────────────────────────────────────────────────

def read_fw(api_url: str, attempts: int = 3) -> dict:
    """Read the inverter's current settings, retrying a flaky datalogger.

    SolarmanV5 serves one connection at a time, so a settings read can lose a
    race with the Prometheus scrape and come back 502. That is routine rather
    than a fault, and giving up would idle the plan for a whole cron interval."""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(f"{api_url}/api/settings", timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == attempts:
                raise
            log.warning("firmware read failed (%s/%s): %s — retrying", attempt, attempts, e)
            time.sleep(3)
    raise RuntimeError("unreachable")


# ── HTTP post ─────────────────────────────────────────────────────────────────

def _post(api_url: str, path: str, body: dict | list, dry_run: bool) -> None:
    url = f"{api_url}{path}"
    if dry_run:
        log.info("[dry] POST %-30s  %s", path, json.dumps(body)); return
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data,
                                  headers={"Content-Type": "application/json",
                                           "X-Dispatcher": "1"},
                                  method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')}") from e


# ── Plan windows → Deye's 24 h partition ──────────────────────────────────────

def _wrapped_windows(tou_slots: list) -> list[dict]:
    """Project plan windows onto the firmware's cyclic 24 h timeline.

    Keeps each window's ORIGINAL plan minutes alongside the wrapped ones: the
    plan horizon runs past its own midnight, so a '24:15–25:00' window lands at
    00:15–01:00 on the daily cycle — correct for the firmware, but it must not
    then look 'already past' at midday when overflow trimming runs."""
    out: list[dict] = []
    for sl in tou_slots:
        s, e = _tm(sl["start"]), _tm(sl["end"])
        if e <= s:
            continue
        ws = s % DAY_MIN
        we = e % DAY_MIN or DAY_MIN          # end-of-day stays 1440, not 0
        pieces = [(ws, we)] if ws < we else [(ws, DAY_MIN), (0, we)]
        for a, b in pieces:
            a, b = _snap(a), min(_snap(b), DAY_MIN)
            if b <= a:                       # a window shorter than the grid
                b = min(a + TOU_TIME_GRID_MIN, DAY_MIN)
            out.append({"start": a, "end": b,
                        "plan_start": s, "plan_end": e,
                        "soc_floor_pct": sl["soc_floor_pct"],
                        "amps": sl.get("amps")})
    out.sort(key=lambda w: w["start"])

    # Merge windows that touch or overlap — two boundaries where the firmware
    # would see no change in behaviour is a wasted register.
    merged: list[dict] = []
    for w in out:
        if merged and w["start"] <= merged[-1]["end"]:
            prev = merged[-1]
            prev["end"]           = max(prev["end"], w["end"])
            prev["plan_end"]      = max(prev["plan_end"], w["plan_end"])
            prev["soc_floor_pct"] = min(prev["soc_floor_pct"], w["soc_floor_pct"])
        else:
            merged.append(dict(w))
    return merged


def _boundaries(windows: list[dict]) -> list[int]:
    """Minutes at which behaviour changes. 00:00 always starts a slot; a window
    ending at 24:00 needs no closing boundary because slot 6 wraps to slot 1."""
    bounds = {0}
    for w in windows:
        bounds.add(w["start"])
        if w["end"] != DAY_MIN:
            bounds.add(w["end"])
    return sorted(bounds)


def _trim_to_budget(windows: list[dict], now_min: int) -> list[dict]:
    """Shed windows until the partition fits TOU_SLOT_COUNT boundaries.

    Windows already finished are shed first — their registers have done their
    job. Only if the remaining future alone still overflows are the LATEST
    windows deferred; they slide in on a later run once an earlier one ends.
    The planner is supposed to never let this happen, hence the warning."""
    wins = list(windows)
    while len(_boundaries(wins)) > TOU_SLOT_COUNT:
        past = [w for w in wins if w["plan_end"] <= now_min]
        if past:
            wins.remove(past[0])
            continue
        dropped = wins.pop()
        log.warning("plan needs %d boundaries for %d registers — deferring window %s–%s",
                    len(_boundaries(windows)), TOU_SLOT_COUNT,
                    _wall(dropped["start"]), _wall(dropped["end"]))
    return wins


def _pad_boundaries(bounds: list[int]) -> list[int]:
    """Grow the boundary list to exactly TOU_SLOT_COUNT.

    Every one of the six registers always holds a value, so a short list would
    leave stale slots active inside the new schedule. Padding splits the widest
    segment at its midpoint: the extra boundary inherits the parameters of the
    segment it splits, so it is a behavioural no-op. With fewer than six
    boundaries the widest segment always spans over 240 minutes, so there is
    always room to split."""
    bounds = list(bounds)
    while len(bounds) < TOU_SLOT_COUNT:
        spans = [(bounds[i + 1] - bounds[i], i) for i in range(len(bounds) - 1)]
        spans.append((DAY_MIN - bounds[-1] + bounds[0], len(bounds) - 1))
        width, idx = max(spans)
        if width < 2 * TOU_TIME_GRID_MIN:
            raise RuntimeError("cannot pad TOU boundaries — schedule too dense")
        extra = _snap(bounds[idx] + width // 2) % DAY_MIN
        if extra in bounds:                  # snapping collided with a neighbour
            extra = (bounds[idx] + TOU_TIME_GRID_MIN) % DAY_MIN
        bounds.append(extra)
        bounds.sort()
    return bounds


def build_desired_slots(tou_slots: list, now_min: int, opts: dict,
                        soc_lo: int, soc_hi: int, sell_power_w: int,
                        events: list | None = None) -> list[dict]:
    """Turn the plan's discharge windows into the six Deye time points.

    A slot's SOC is a TARGET the inverter drives the battery toward, never a
    passive floor, so each slot gets a deliberate one:

      selling window   the plan's floor for that window — drain to it and sell.
      export possible  the soft maximum. The plan wants surplus *solar* sold
                       here but not the battery, and a target above the current
                       level is the only way to say that: with sell power open,
                       a low target would empty the pack into the grid.
      otherwise        the battery minimum, so it can still carry the house.
                       Safe because export power is 0 W throughout.

    Splitting the last two off the plan's export timeline costs no boundaries —
    it only changes what value an already-existing slot carries."""
    windows = _trim_to_budget(_wrapped_windows(tou_slots), now_min)
    bounds  = _pad_boundaries(_boundaries(windows))
    # Only relevant when solar is sold outside the windows; otherwise sell power
    # is 0 W there and a low target cannot leak anything to the grid.
    mask    = export_mask(events or []) if opts["sell_solar_outside_windows"] else [False] * DAY_MIN

    slots = []
    for n, start in enumerate(bounds, start=1):
        win = next((w for w in windows if w["start"] <= start < w["end"]), None)
        selling = win is not None
        nxt     = bounds[n % len(bounds)]
        if selling:
            target = win["soc_floor_pct"]
        elif _exports_during(mask, start, nxt):
            target = soc_hi
        else:
            target = soc_lo
        slot = {
            "slot":        n,
            "time":        _wall(start),
            "power_w":     sell_power_w if selling else opts["inverter_power_w"],
            "soc_pct":     _clamp_soc(target, soc_lo, soc_hi),
            # Never charge from grid or generator: the planner has no such
            # concept, and a set grid-charge bit would flip this slot's SOC
            # field from "discharge down to" into "charge up to".
            "grid_charge": False,
            "gen_charge":  False,
        }
        if opts["write_sell_bit"]:
            slot["sell"] = selling
        slots.append(slot)
    return slots


# ── State / pending verification ──────────────────────────────────────────────

def _slots_hash(desired: list) -> str:
    return hashlib.sha1(json.dumps(desired, sort_keys=True).encode()).hexdigest()[:10]

def load_state() -> dict:
    return json.loads(_STATE_FILE.read_text()) if _STATE_FILE.exists() else {}

def save_state(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state, indent=2))


def _slot_matches(fw: dict, want: dict) -> bool:
    for key in ("time", "soc_pct", "grid_charge", "gen_charge", "sell"):
        if key in want and fw.get(key) != want[key]:
            return False
    return "power_w" not in want or round(fw.get("power_w", -1)) == round(want["power_w"])


def verify_pending(pending: dict, fw: dict, battery_count: int, cap: int = 0) -> bool:
    """Compare pending writes against firmware; log result. True if all confirmed."""
    fw_general = fw.get("general") or {}
    fw_battery = fw.get("battery") or {}
    fw_tou     = fw.get("tou") or {}
    fw_slots   = {s["slot"]: s for s in fw_tou.get("slots", [])}
    ok, miss   = [], []

    if "export_mode" in pending:
        matched = fw_general.get("limit_control") == pending["export_mode"]
        if "solar_sell" in pending:
            matched = matched and fw_general.get("solar_sell_enable") is pending["solar_sell"]
        (ok if matched else miss).append("export")

    if "charge_amps" in pending:
        per     = _per_bat(pending["charge_amps"], battery_count, cap)
        matched = fw_battery.get("max_charge_a") == per
        if battery_count >= 2:
            matched = matched and fw_battery.get("bat2_max_charge_a") == per
        (ok if matched else miss).append("charge_amps")

    if "tou_slots" in pending:
        matched = all(_slot_matches(fw_slots.get(d["slot"], {}), d)
                      for d in pending["tou_slots"])
        (ok if matched else miss).append("tou_slots")

    if "tou_enabled" in pending:
        (ok if fw_tou.get("enabled") == pending["tou_enabled"] else miss).append("tou_enabled")

    if ok:   log.info("pending confirmed: %s", ok)
    if miss: log.warning("pending NOT applied (will retry): %s", miss)
    return not miss


# ── TOU sync ──────────────────────────────────────────────────────────────────

def sync_tou(desired: list, fw_slots: list, api_url: str, dry_run: bool) -> bool:
    fw = {s["slot"]: s for s in fw_slots}
    if all(_slot_matches(fw.get(d["slot"], {}), d) for d in desired):
        log.debug("tou slots: up to date"); return False

    log.info("tou slots: %s", [(d["slot"], d["time"],
                                "sell" if d.get("sell") else f"idle {d['soc_pct']}%")
                               for d in desired])
    _post(api_url, "/api/settings/tou/all", desired, dry_run)
    return True


def ensure_tou_enabled(fw_tou: dict, api_url: str, dry_run: bool) -> bool:
    """The whole schedule is inert unless register 146's enable bit is set."""
    if fw_tou.get("enabled"):
        return False
    log.warning("tou schedule was disabled — enabling")
    _post(api_url, "/api/settings/general", {"tou_enabled": True}, dry_run)
    return True


# ── Export ────────────────────────────────────────────────────────────────────

def exports_now(want: bool, sell_kw: float, in_window: bool) -> bool:
    """Whether the plant should be exporting at this moment.

    `in_window` is False outside a planned battery-sell window while the plant
    is configured not to sell solar there. Deye cannot separate "sell surplus
    solar" from "sell the battery" — one work mode governs both, against a slot
    target that also decides whether the battery may serve the house — so
    declining to export outside the windows is the only way to leave the battery
    free for the load."""
    return bool(want) and sell_kw > 0 and in_window


def export_power_w(sell_kw: float, cap_w: int) -> int:
    """Ceiling to use while selling.

    A plant's grid export limit always wins: the plan's sell_kw is a
    planner-side figure that knows nothing about the connection agreement."""
    target = int(sell_kw * 1000)
    if cap_w > 0 and target > cap_w:
        log.info("max_sell_power: plan wants %d W, capped to %d W", target, cap_w)
        target = cap_w
    return target


def apply_export(want: bool, fw_general: dict, sell_kw: float, cap_w: int,
                 api_url: str, dry_run: bool, in_window: bool = True) -> bool:
    """Switch export via the work mode, and solar-sell by price.

    Two different questions, two different registers:

      solar_sell_enable  Is selling worth it right now?  That is purely the
                         price call, and the plan has already made it — the
                         map's export events are exactly `ibex >= min_sell_price`.
      limit_control      May anything leave the plant at all?  This is the one
                         that also puts the battery on the market, so it opens
                         only where draining is actually intended.

    Keeping them separate means the price signal is honoured verbatim while the
    battery stays governed by the plan's own windows."""
    on        = exports_now(want, sell_kw, in_window)
    want_mode = EXPORT_ON_MODE if on else EXPORT_OFF_MODE
    body: dict = {}
    if fw_general.get("limit_control") != want_mode:
        body["limit_control"] = want_mode
    if fw_general.get("solar_sell_enable") is not bool(want):
        body["solar_sell_enable"] = bool(want)
    if on:
        target = export_power_w(sell_kw, cap_w)
        if fw_general.get("max_sell_power_w") != target:
            body["max_sell_power_w"] = target
    if not body:
        log.debug("export: unchanged (%s)", want_mode); return False
    log.info("export: %s → %s  %s", fw_general.get("limit_control"), want_mode, body)
    _post(api_url, "/api/settings/general", body, dry_run)
    return True


# ── Charge amps ───────────────────────────────────────────────────────────────

def full_charge_amps(events: list, fallback: float) -> float:
    """The plan's unthrottled charge current.

    The planner drops charge_amps to ~1 A whenever it expects to be selling, so
    that surplus solar goes to the grid instead of the battery. When the plant
    declines to sell outside its windows that throttle would simply waste the
    solar, so the highest figure the plan uses is restored instead."""
    amps = [e["charge_amps"] for e in events if "charge_amps" in e]
    return max(amps) if amps else fallback


def _per_bat(amps: float, battery_count: int, cap: int = 0) -> int:
    """Deye's charge-current registers are whole amps, unlike Solis's ×10.

    `cap` is the per-battery ceiling from config. The plan's charge_amps is a
    planner-side figure that knows nothing about this pack's rating, so a plant
    whose battery accepts 40 A can easily be handed 100 A."""
    per = max(1, round(amps / battery_count))
    return min(per, cap) if cap > 0 else per


def apply_charge_amps(total_a: float, fw_battery: dict | None, battery_count: int,
                      cap: int, api_url: str, dry_run: bool) -> bool:
    per  = _per_bat(total_a, battery_count, cap)
    body = {"max_charge_a": per}
    if battery_count >= 2:
        body["bat2_max_charge_a"] = per
    if fw_battery and all(fw_battery.get(k) == v for k, v in body.items()):
        log.debug("charge_amps: unchanged (%d A × %d)", per, battery_count); return False
    capped = " (capped)" if cap > 0 and round(total_a / battery_count) > cap else ""
    log.info("charge_amps: %.0f A total → %d A each%s (%d batteries)",
             total_a, per, capped, battery_count)
    _post(api_url, "/api/settings/battery", body, dry_run)
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


def _active_window(tou_slots: list, now_min: int) -> dict | None:
    for w in _wrapped_windows(tou_slots):
        if w["start"] <= now_min % DAY_MIN < w["end"]:
            return w
    return None


def _soc_guard_would_fire(tou_slots: list, now_min: int, soc: int | None) -> bool:
    if soc is None: return False
    w = _active_window(tou_slots, now_min)
    return w is not None and soc <= w["soc_floor_pct"] + 2


def soc_guard(tou_slots: list, desired: list, now_min: int, soc: int | None,
              opts: dict, api_url: str, dry_run: bool) -> bool:
    """Stop an in-progress drain that has reached its floor.

    Deye enforces the slot's SOC floor itself, so this is a backstop rather
    than the primary mechanism it is on Solis. Raising the live slot's floor to
    the measured SoC halts the discharge without disturbing the other slots."""
    if soc is None: return False
    w = _active_window(tou_slots, now_min)
    if w is None or soc > w["soc_floor_pct"] + 2:
        return False

    now = now_min % DAY_MIN
    live = None
    for i, d in enumerate(desired):
        end = desired[i + 1]["time"] if i + 1 < len(desired) else desired[0]["time"]
        start_m, end_m = _tm(d["time"]), _tm(end)
        inside = (start_m <= now < end_m) if start_m < end_m else (now >= start_m or now < end_m)
        if inside:
            live = d
            break
    if live is None:
        return False

    log.warning("soc guard: %d%% ≤ floor %d%% + 2 — halting discharge in slot %d",
                soc, w["soc_floor_pct"], live["slot"])
    body: dict = {"soc_pct": min(100, max(0, soc))}
    if opts["write_sell_bit"]:
        body["sell"] = False
    _post(api_url, f"/api/settings/tou/{live['slot']}", body, dry_run)
    return True


# ── Show map ─────────────────────────────────────────────────────────────────

_CIRCLE = ["①","②","③","④","⑤","⑥"]
_RST  = "\033[0m"
_BOLD = "\033[1m"
_DIM  = "\033[2m"
_GRN  = "\033[32m"
_YLW  = "\033[33m"
_CYN  = "\033[36m"
_RED  = "\033[31m"

def show_map(m: dict, now_min: int, opts: dict) -> None:
    gen = m.get("generated_at", "")
    print(f"\n{_BOLD}Map:{_RST}  {m.get('date','')}  {m.get('instance_id','')}  "
          f"[{m.get('algo','')}]  generated {gen[11:19] if len(gen) > 10 else gen}")

    tou = m.get("tou_slots", [])
    print(f"\n{_BOLD}Plan discharge windows:{_RST}")
    if tou:
        for i, sl in enumerate(tou):
            print(f"  {_CYN}{_CIRCLE[i % len(_CIRCLE)]}{_RST}  {sl['start']} – {sl['end']}"
                  f"   {_YLW}{sl['amps']} A{_RST}  floor {sl['soc_floor_pct']}%")
    else:
        print(f"  {_DIM}(none){_RST}")

    sell_kw   = float(m.get("sell_kw", 0))
    sell_w    = min(int(sell_kw * 1000), opts["inverter_power_w"]) if sell_kw > 0 \
                else opts["inverter_power_w"]
    soc_lo, soc_hi = soc_envelope(m, opts)
    desired   = build_desired_slots(tou, now_min, opts, soc_lo, soc_hi, sell_w,
                                    m.get("events", []))

    print(f"\n{_BOLD}Deye TOU slots (24 h partition):{_RST}")
    for i, d in enumerate(desired):
        end   = desired[i + 1]["time"] if i + 1 < len(desired) else desired[0]["time"]
        mark  = f"{_GRN}SELL{_RST}" if d.get("sell") else f"{_DIM}idle{_RST}"
        print(f"  {_CYN}{_CIRCLE[i]}{_RST}  {d['time']} → {end}   {mark}"
              f"   {_YLW}{d['power_w']} W{_RST}  soc {d['soc_pct']}%")

    events = m.get("events", [])
    want   = desired_state(events, now_min)
    active_time = None
    for ev in events:
        if _tm(ev["time"]) <= now_min + EVENT_TOLERANCE_MIN:
            active_time = ev["time"]

    print(f"\n{_BOLD}Events:{_RST}")
    for ev in events:
        t, parts = ev["time"], []
        if "export" in ev:
            parts.append(f"{_GRN}export ON{_RST}" if ev["export"] else f"{_RED}export OFF{_RST}")
        if "charge_amps" in ev:
            parts.append(f"charge → {_YLW}{ev['charge_amps']} A{_RST}")
        if "soc_floor_pct" in ev:
            parts.append(f"floor {ev['soc_floor_pct']}%")
        is_now = (t == active_time)
        marker = f" {_BOLD}◀ now{_RST}" if is_now else ""
        print(f"  {_BOLD if is_now else ''}{t}{_RST if is_now else ''}   {'  '.join(parts)}{marker}")

    exp_on = exports_now(bool(want.get("export")), sell_kw,
                         opts["sell_solar_outside_windows"]
                         or _active_window(tou, now_min) is not None)
    exp_w  = export_power_w(sell_kw, opts["max_sell_power_w"]) if exp_on else 0
    print(f"\n{_DIM}now = {_fmt(now_min)}   soc envelope {soc_lo}-{soc_hi}%   "
          f"desired: export={want.get('export','?')} ({exp_w} W)  "
          f"charge_amps={want.get('charge_amps','?')}{_RST}\n")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Solar plan dispatcher (Deye)")
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

    # The plan's horizon can run up to 1 h past its own midnight (map date=D
    # covers D 00:00 through D+1 01:00, e.g. events at "24:15"). If we're still
    # serving yesterday's map — today's hasn't been generated/pushed yet — shift
    # now_min onto that map's timeline so those post-midnight entries become
    # reachable. Only exactly-yesterday qualifies; anything older is already the
    # stale-data situation the load_map fallback warns about.
    if not args.time and m.get("date") == (date.today() - timedelta(days=1)).isoformat():
        now_min += DAY_MIN
        log.info("serving previous day's map (%s) — now shifted to %s",
                 m["date"], _fmt(now_min))

    if args.show:
        show_map(m, now_min, cfg); return

    log.info("run at %s", _fmt(now_min))

    tou_slots   = m.get("tou_slots", [])
    sell_kw     = float(m.get("sell_kw", 0))
    instance_id = m.get("instance_id", "")
    sell_w      = (min(int(sell_kw * 1000), cfg["inverter_power_w"]) if sell_kw > 0
                   else cfg["inverter_power_w"])
    soc_lo, soc_hi = soc_envelope(m, cfg)

    want = desired_state(m.get("events", []), now_min)
    if want:
        log.info("desired: %s", want)

    soc = get_soc(cfg["prom_url"], instance_id)
    if soc is not None:
        log.info("soc: %d%%", soc)

    desired = build_desired_slots(tou_slots, now_min, cfg, soc_lo, soc_hi, sell_w,
                                 m.get("events", []))

    # Outside a planned window the plant may be configured to keep sell power at
    # 0 W, which is what leaves the battery free to serve the house.
    in_window = (cfg["sell_solar_outside_windows"]
                 or _active_window(tou_slots, now_min) is not None)

    # ── Early exit if nothing has changed since last run ──────────────────────
    # in_window is part of the fingerprint: leaving a window changes what gets
    # written without necessarily changing the plan's own desired state.
    state       = load_state()
    fingerprint = (f"{_slots_hash(desired)}|{json.dumps(want, sort_keys=True)}"
                   f"|{int(in_window)}")
    if (not state.get("pending")
            and state.get("fingerprint") == fingerprint
            and not _soc_guard_would_fire(tou_slots, now_min, soc)):
        log.debug("desired state unchanged — skipping firmware read")
        return

    try:
        fw = read_fw(cfg["api_url"])
    except Exception as e:
        log.error("cannot read firmware: %s", e); sys.exit(1)

    fw_general = fw.get("general") or {}
    fw_tou     = fw.get("tou") or {}
    fw_slots   = fw_tou.get("slots", [])
    fw_battery = fw.get("battery")

    n_bat = resolve_battery_count(cfg, fw_battery)

    pending = state.get("pending")
    if pending and verify_pending(pending, fw, n_bat, cfg["max_charge_amps"]):
        state["pending"] = None

    en_hit  = ensure_tou_enabled(fw_tou, cfg["api_url"], args.dry_run)
    soc_hit = soc_guard(tou_slots, desired, now_min, soc, cfg, cfg["api_url"], args.dry_run)

    # The guard has just deliberately overridden the live slot; re-writing the
    # full schedule in the same run would undo it.
    tou_hit = False if soc_hit else sync_tou(desired, fw_slots, cfg["api_url"], args.dry_run)

    # Export is applied unconditionally: a map with no reachable export event is
    # malformed, and the safe reading of "the plan did not say" is "do not sell"
    # rather than leaving a stale sell power in place.
    exp_hit = apply_export(bool(want.get("export")), fw_general, sell_kw,
                           cfg["max_sell_power_w"], cfg["api_url"], args.dry_run,
                           in_window)

    chg_hit = False
    if "charge_amps" in want:
        charge_a = want["charge_amps"]
        if not in_window:
            full = full_charge_amps(m.get("events", []), charge_a)
            if full > charge_a:
                log.info("charge_amps: %.0f A → %.0f A — not selling here, so bank the solar",
                         charge_a, full)
                charge_a = full
        chg_hit = apply_charge_amps(charge_a, fw_battery, n_bat,
                                    cfg["max_charge_amps"], cfg["api_url"], args.dry_run)

    if not any([en_hit, soc_hit, tou_hit, exp_hit, chg_hit]):
        log.info("all up to date")

    if not args.dry_run:
        new_pending: dict = {}
        if tou_hit: new_pending["tou_slots"]   = desired
        if exp_hit:
            new_pending["export_mode"] = (
                EXPORT_ON_MODE if exports_now(bool(want.get("export")), sell_kw, in_window)
                else EXPORT_OFF_MODE)
            new_pending["solar_sell"] = bool(want.get("export"))
        if chg_hit: new_pending["charge_amps"] = charge_a
        if en_hit:  new_pending["tou_enabled"] = True
        save_state({
            "date":        date.today().isoformat(),
            "fingerprint": None if soc_hit else fingerprint,
            "pending":     new_pending or None,
        })


if __name__ == "__main__":
    main()
