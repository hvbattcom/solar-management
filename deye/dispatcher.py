#!/usr/bin/env python3
"""
dispatcher.py — Solar plan event dispatcher for Deye (5-min cron).

Same job as solis/dispatcher.py — turn the planner's dispatch map into inverter
settings — but Deye needs a different shape entirely, because its Time-of-Use
registers do not describe a schedule the way Solis's do.

Deye has 6 TOU time POINTS tiling the day, with no per-slot enable. Encoding the
plan's windows as boundaries costs two registers per window, caps the day at
about three windows, and makes every window edge a register rewrite. So this
dispatcher does not put the schedule in the inverter at all:

    slot TIMES are fixed scaffolding, written once and never touched
    slot VALUES are the live control signal, rewritten when intent changes

The plan is executed here, every five minutes, against two controls:

    the two gates     work mode (142) and the per-slot sell bit, ORed: power
                      leaves if either is open, so both are closed together.

    target vs SoC     what the open gate is allowed to draw from. Below the
                      current level the battery is dispatched; at or above it
                      the pack is held and only surplus PV can leave.

Three states, and the unsafe pairing — gate open with a low target outside a
planned window — is unreachable from all of them:

    carrying the house   gate closed, target low   (pack carries the load)
    selling surplus      gate open,   target ≥ SoC (only PV can leave)
    selling the battery  gate open,   target = the window's floor

Windows therefore cost nothing. Twelve of them are the same as two, resolved at
5-minute granularity, and a window landing next to a slot boundary — which used
to be a real hazard — no longer means anything.

All six slots are written with identical values, so crossing a boundary between
runs is a no-op. The write is read back and confirmed before the gate is opened:
a half-applied block once left two slots selling with nothing logged.

Crontab:  */5 * * * *  /usr/bin/python3 /path/to/dispatcher.py

No redirection needed — output goes to dispatcher.log (rotated), and the
terminal echo is suppressed when stdout is not a TTY, so cron stays quiet.
"""

import argparse
import configparser
import json
import logging
import sys
import time
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

# Fixed scaffolding. The values are arbitrary — every slot carries identical
# settings, so where the boundaries fall changes nothing. They exist only
# because the hardware insists on six of them.
SCAFFOLD_TIMES = ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"]

# Export has TWO gates and they are an OR: it flows if the work mode is open or
# if the live slot's sell bit is set. Measured on 2026-08-03, all three cases:
#   all bits cleared, mode open      -> exported 24 kW off the pack at night
#   bits set, mode Zero Export to CT -> exported 23 kW
#   both closed                      -> stopped within seconds
# So neither alone is sufficient to stop it, and both are driven together. All
# six slots carry the same bit: setting only the live one would leave a boundary
# crossing to flip selling on or off for up to a whole dispatcher interval.
MODE_EXPORT = "Selling First"
MODE_CLOSED = "Zero Export to CT"

# Surplus is measured, not inferred from the clock: a cloudy afternoon needs the
# battery on the house exactly as much as midnight does.
#
#   surplus = -(battery + grid)   — what is going INTO the pack plus what is
#   leaving the plant, i.e. whatever the load did not take.
#
# Two thresholds rather than one, because a pack released and held again every
# five minutes would thrash both the registers and the house supply.
HOLD_ABOVE_W    = 300    # comfortably in surplus  → hold the pack
RELEASE_BELOW_W = 0      # drawing from the grid   → release it to the house

# The map's times are the PLANNER's local time, and the dispatcher compares them
# against its own clock. A host on a different timezone therefore executes the
# whole plan by that offset — a UTC plant ran a 21:00 window at 00:24 local and
# would have kept selling for three more hours. `generated_at` is the planner's
# local timestamp, so comparing it to our clock catches the skew directly.
CLOCK_SKEW_LIMIT_MIN = 90

SOC_GUARD_MARGIN = 2     # stop a drain this far above the plan's floor
SOC_MIN_FALLBACK = 20    # only for a map with no envelope at all


# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_log() -> logging.Logger:
    logger = logging.getLogger("deye-dispatcher")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh  = RotatingFileHandler(_LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    # Echo to the terminal only when there is one, so cron needs no redirection.
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


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(str(path))
    srv = cfg["DeyeAPI"]      if "DeyeAPI"      in cfg else {}
    inv = cfg["DeyeInverter"] if "DeyeInverter" in cfg else {}
    raw_count = str(srv.get("battery_count", "auto")).strip().lower()
    return {
        "api_url":          f"http://localhost:{srv.get('port', 5000)}",
        "battery_count":    None if raw_count in ("", "auto") else max(1, int(raw_count)),
        "max_charge_amps":  int(srv.get("max_charge_amps", 0)),
        "max_sell_power_w": int(srv.get("max_sell_power_w", 0)),
        "inverter_power_w": int(float(inv.get("inverter_power_kw", 30)) * 1000),
    }


# ── Map ───────────────────────────────────────────────────────────────────────

def load_map(instance_id: str | None = None) -> dict:
    today   = date.today().isoformat()
    pattern = f"map_{today}_{instance_id}.json" if instance_id else f"map_{today}_*.json"
    candidates = sorted(_MAPS_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        candidates = sorted(_MAPS_DIR.glob("map_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            log.error("no map found in %s", _MAPS_DIR); sys.exit(1)
        log.warning("no map for today — using %s", candidates[0].name)
    m = json.loads(candidates[0].read_text())
    log.info("map %s: %d events, %d windows", candidates[0].name,
             len(m.get("events", [])), len(m.get("tou_slots", [])))
    return m


def soc_envelope(m: dict) -> tuple[int, int]:
    """The battery's operating range, from the planner via the map."""
    lo = m.get("soc_min_pct")
    if lo is None:
        lo = SOC_MIN_FALLBACK
        log.warning("map carries no SoC envelope — falling back to %d%%; "
                    "is the planner up to date?", lo)
    hi = m.get("soc_max_pct") or 100
    lo, hi = int(lo), int(hi)
    return (max(0, min(lo, hi)), min(100, max(lo, hi)))


def clock_skew_min(m: dict, now: datetime) -> float | None:
    """Minutes our clock is behind the planner's, from a map's own timestamp.

    Negative means we are ahead. None when the map carries no usable stamp."""
    stamp = m.get("generated_at")
    if not stamp:
        return None
    try:
        return (datetime.fromisoformat(stamp) - now).total_seconds() / 60.0
    except ValueError:
        return None


def newest_map_skew(now: datetime) -> tuple[float | None, str]:
    """Skew measured against the most recently pushed map, whichever day it is for.

    Deliberately not the map being dispatched: a host behind the planner selects
    by its own date, so once the planner has rolled past midnight it keeps
    picking yesterday's map — whose timestamp is comfortably in the past and
    hides the very skew that made the selection wrong."""
    newest = max(_MAPS_DIR.glob("map_*.json"), key=lambda p: p.stat().st_mtime, default=None)
    if newest is None:
        return None, ""
    try:
        data = json.loads(newest.read_text())
    except Exception:
        return None, newest.name
    return clock_skew_min(data, now), data.get("generated_at", "")


def desired_state(events: list, now_min: int) -> dict:
    """Walk events up to now+tolerance; return the last value of each field."""
    state: dict = {}
    for ev in events:
        if _tm(ev["time"]) > now_min + EVENT_TOLERANCE_MIN:
            break
        if "export"      in ev: state["export"]      = ev["export"]
        if "charge_amps" in ev: state["charge_amps"] = ev["charge_amps"]
    return state


def active_window(tou_slots: list, now_min: int) -> dict | None:
    """The plan's selling window covering this moment, if any.

    The plan horizon runs past its own midnight ('24:15'), so both the window
    and the clock are read on a 24 h cycle."""
    now = now_min % DAY_MIN
    for sl in tou_slots:
        s, e = _tm(sl["start"]), _tm(sl["end"])
        if e <= s:
            continue
        ws, we = s % DAY_MIN, (e % DAY_MIN) or DAY_MIN
        inside = ws <= now < we if ws < we else (now >= ws or now < we)
        if inside:
            return sl
    return None


def full_charge_amps(events: list, fallback: float) -> float:
    """The plan's unthrottled charge current.

    The planner drops charge_amps to ~1 A whenever it expects to be selling, so
    surplus solar goes to the grid rather than the battery. Outside a selling
    window that throttle would only waste the solar."""
    amps = [e["charge_amps"] for e in events if "charge_amps" in e]
    return max(amps) if amps else fallback


# ── Inverter I/O ──────────────────────────────────────────────────────────────

def _get(api_url: str, path: str, attempts: int = 3) -> dict:
    """SolarmanV5 serves one connection at a time, so losing a race with the
    Prometheus scrape is routine rather than a fault."""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(f"{api_url}{path}", timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == attempts:
                raise
            log.warning("%s failed (%d/%d): %s — retrying", path, attempt, attempts, e)
            time.sleep(3)
    raise RuntimeError("unreachable")


def read_live(api_url: str) -> dict:
    """SoC and the power balance, straight from the inverter."""
    st   = _get(api_url, "/api/status")
    batt = st.get("battery") or {}
    return {
        "soc":     int(batt.get("soc_pct", 0)),
        "batt_w":  float(batt.get("power_w", 0)),      # positive = discharging
        "grid_w":  float((st.get("grid") or {}).get("total_power_w", 0)),  # positive = importing
        "pv_w":    float((st.get("pv") or {}).get("total_power_w", 0)),
    }


def surplus_w(live: dict) -> float:
    """Power the load did not take: what is charging the pack plus what is
    leaving the plant. Negative means the house is pulling from the grid."""
    return -(live["batt_w"] + live["grid_w"])


def _post(api_url: str, path: str, body, dry_run: bool) -> None:
    if dry_run:
        log.info("[dry] POST %-28s  %s", path, json.dumps(body)); return
    req = urllib.request.Request(
        f"{api_url}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Dispatcher": "1"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')}") from e


# ── The decision ──────────────────────────────────────────────────────────────

def hold_pack(live: dict, previous: bool | None) -> bool:
    """Should the battery be held rather than allowed to carry the house?

    Held while there is surplus to bank, released once the house starts drawing
    from the grid. Between the two thresholds the previous answer stands, so a
    plant hovering at the margin does not flip every five minutes."""
    s = surplus_w(live)
    if s >= HOLD_ABOVE_W:
        return True
    if s <= RELEASE_BELOW_W:
        return False
    return bool(previous)


def decide(m: dict, now_min: int, live: dict, opts: dict, previous_hold: bool | None) -> dict:
    """Everything the inverter should be doing at this moment.

    Three states, and the unsafe combination — export open while the target sits
    below SoC outside a planned window — cannot be reached from any of them:

      SELL BATTERY   in a window, price good: mode open, target at the plan's
                     floor, so the pack drains to it and no further.
      SELL SURPLUS   price good and there is surplus to sell: mode open, but the
                     target is held at or above SoC, so the battery cannot be
                     the source and only PV leaves.
      HOUSE          everything else: mode closed, target low, pack free to
                     carry the load with nothing able to reach the grid.
    """
    events   = m.get("events", [])
    want     = desired_state(events, now_min)
    price_ok = bool(want.get("export"))
    lo, hi   = soc_envelope(m)
    win      = active_window(m.get("tou_slots", []), now_min)

    sell_kw = float(m.get("sell_kw", 0))
    ceiling = int(sell_kw * 1000) if sell_kw > 0 else opts["inverter_power_w"]
    if opts["max_sell_power_w"] > 0:
        ceiling = min(ceiling, opts["max_sell_power_w"])

    charge_a = want.get("charge_amps")
    base = {"ceiling": ceiling, "solar_sell": price_ok, "charge_a": charge_a}

    if win and price_ok:
        floor = max(lo, min(hi, int(win["soc_floor_pct"])))
        if live["soc"] <= floor + SOC_GUARD_MARGIN:
            log.warning("soc guard: %d%% is within %d%% of the floor %d%% — closing export",
                        live["soc"], SOC_GUARD_MARGIN, floor)
            return {**base, "mode_on": False, "target": lo, "sell": False,
                    "hold": False, "reason": "window (guarded at the floor)"}
        return {**base, "mode_on": True, "target": floor, "sell": True,
                "hold": False, "reason": "window: selling the battery"}

    hold = hold_pack(live, previous_hold)
    if price_ok and hold:
        # Worth selling and there is surplus to sell. The target is pinned at or
        # above where the pack already sits, so opening the mode cannot drain it.
        target = max(hi, min(100, live["soc"]))
        return {**base, "mode_on": True, "target": target, "sell": False,
                "hold": True, "reason": "selling solar surplus"}

    if charge_a is not None and not hold:
        charge_a = max(charge_a, full_charge_amps(events, charge_a))
    return {**base, "charge_a": charge_a, "mode_on": False,
            "target": hi if hold else lo, "sell": False, "hold": hold,
            "reason": "banking surplus" if hold else "carrying the house"}


# ── Applying it ───────────────────────────────────────────────────────────────

def _per_bat(amps: float, battery_count: int, cap: int = 0) -> int:
    """Deye's charge-current registers are whole amps, unlike Solis's ×10."""
    per = max(1, round(amps / battery_count))
    return min(per, cap) if cap > 0 else per


def resolve_battery_count(opts: dict, fw_battery: dict | None) -> int:
    """A second battery that is not installed reports 0 A limits."""
    if opts["battery_count"]:
        return opts["battery_count"]
    return 2 if (fw_battery or {}).get("bat2_max_charge_a") else 1


def slots_match(plan: dict, fw_tou: dict, opts: dict) -> bool:
    fw = {s["slot"]: s for s in fw_tou.get("slots", [])}
    for w in desired_slots(plan, opts):
        f = fw.get(w["slot"], {})
        if (f.get("time") != w["time"] or f.get("soc_pct") != w["soc_pct"]
                or bool(f.get("sell")) != w["sell"]
                or bool(f.get("grid_charge")) or bool(f.get("gen_charge"))
                or round(f.get("power_w", -1)) != round(w["power_w"])):
            return False
    return True


def desired_slots(plan: dict, opts: dict) -> list:
    return [{"slot": n + 1, "time": SCAFFOLD_TIMES[n],
             "power_w": opts["inverter_power_w"],
             "soc_pct": plan["target"], "sell": plan["sell"],
             "grid_charge": False, "gen_charge": False}
            for n in range(TOU_SLOT_COUNT)]


def sync_slots(plan: dict, fw_tou: dict, opts: dict, api_url: str,
               dry_run: bool, attempts: int = 2) -> tuple[bool, bool]:
    """Write all six slots identically, then read back and confirm.

    Verification is not optional. A write of this block goes out one register at
    a time, and a run that stopped half way through once left two slots still
    selling with nothing logged — the live slot kept the stale value and the
    plant exported for a further fifteen minutes. Since every slot is supposed
    to be identical, a partial write breaks the very invariant the design rests
    on, so the caller is told whether the schedule is trustworthy."""
    if slots_match(plan, fw_tou, opts):
        log.debug("slots: up to date (target %d%%, sell %s)", plan["target"], plan["sell"])
        return False, True

    want = desired_slots(plan, opts)
    log.info("slots → target %d%%  sell %s  (%s)", plan["target"], plan["sell"], plan["reason"])
    for attempt in range(1, attempts + 1):
        _post(api_url, "/api/settings/tou/all", want, dry_run)
        if dry_run:
            return True, True
        fresh = _get(api_url, "/api/settings").get("tou") or {}
        if slots_match(plan, fresh, opts):
            return True, True
        log.warning("slots did not verify (attempt %d/%d) — rewriting", attempt, attempts)
    log.error("slots still do not match after %d attempts", attempts)
    return True, False


def sync_mode(mode_on: bool, fw_general: dict, api_url: str, dry_run: bool) -> bool:
    """The export gate. Written on its own so the caller controls the ordering:
    closed before the schedule changes, opened only after it has verified."""
    want = MODE_EXPORT if mode_on else MODE_CLOSED
    if fw_general.get("limit_control") == want:
        return False
    log.info("mode → %s", want)
    _post(api_url, "/api/settings/general", {"limit_control": want}, dry_run)
    return True


def sync_general(plan: dict, fw_general: dict, api_url: str, dry_run: bool) -> bool:
    """Solar-sell follows the plan's price signal; the ceiling follows its
    sell power. Neither can export anything on its own — that is the mode."""
    body = {}
    if fw_general.get("solar_sell_enable") is not plan["solar_sell"]:
        body["solar_sell_enable"] = plan["solar_sell"]
    if fw_general.get("max_sell_power_w") != plan["ceiling"]:
        body["max_sell_power_w"] = plan["ceiling"]
    if not body:
        log.debug("general: up to date"); return False
    log.info("general → %s", body)
    _post(api_url, "/api/settings/general", body, dry_run)
    return True


def sync_battery(plan: dict, fw_battery: dict | None, n_bat: int, opts: dict,
                 api_url: str, dry_run: bool) -> bool:
    if plan["charge_a"] is None:
        return False
    per  = _per_bat(plan["charge_a"], n_bat, opts["max_charge_amps"])
    body = {"max_charge_a": per}
    if n_bat >= 2:
        body["bat2_max_charge_a"] = per
    if fw_battery and all(fw_battery.get(k) == v for k, v in body.items()):
        log.debug("battery: unchanged (%d A × %d)", per, n_bat); return False
    log.info("battery → %d A charge each (%d batteries)", per, n_bat)
    _post(api_url, "/api/settings/battery", body, dry_run)
    return True


def ensure_tou_enabled(fw_tou: dict, api_url: str, dry_run: bool) -> bool:
    """None of this means anything unless register 146 has the schedule on."""
    if fw_tou.get("enabled"):
        return False
    log.warning("tou schedule was disabled — enabling")
    _post(api_url, "/api/settings/general", {"tou_enabled": True}, dry_run)
    return True


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Show ──────────────────────────────────────────────────────────────────────

def show(m: dict, now_min: int, live: dict, plan: dict) -> None:
    lo, hi = soc_envelope(m)
    gen    = m.get("generated_at", "")
    print(f"\nMap  {m.get('date','')}  {m.get('instance_id','')}  [{m.get('algo','')}]  "
          f"generated {gen[11:19] if len(gen) > 10 else gen}")
    print(f"SoC envelope {lo}–{hi}%   sell ceiling {plan['ceiling']} W")

    print("\nPlan windows:")
    for sl in m.get("tou_slots", []) or [None]:
        print(f"  {sl['start']} – {sl['end']}   floor {sl['soc_floor_pct']}%"
              if sl else "  (none)")

    print(f"\nLive   soc {live['soc']}%   pv {live['pv_w']:.0f} W   "
          f"batt {live['batt_w']:+.0f} W   grid {live['grid_w']:+.0f} W   "
          f"surplus {surplus_w(live):+.0f} W")
    print(f"\nNow {_fmt(now_min % DAY_MIN)} → {plan['reason']}")
    print(f"  target      {plan['target']}%   ({'hold' if plan['hold'] else 'release'})")
    print(f"  sell bit    {plan['sell']}")
    print(f"  solar sell  {plan['solar_sell']}")
    print(f"  charge      {plan['charge_a']} A\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Solar plan dispatcher (Deye)")
    ap.add_argument("--config",   default=str(_DEFAULT_CFG))
    ap.add_argument("--dry-run",  action="store_true")
    ap.add_argument("--time",     default=None, help="Override current time HH:MM")
    ap.add_argument("--instance", default=None, help="instance_id to load")
    ap.add_argument("--show",     action="store_true", help="Print state and exit")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    m   = load_map(args.instance)

    _now    = datetime.now()
    now_min = _tm(args.time) if args.time else _now.hour * 60 + _now.minute
    # A map still serving yesterday covers up to 1 h past its own midnight.
    if not args.time and m.get("date") == (date.today() - timedelta(days=1)).isoformat():
        now_min += DAY_MIN
        log.info("serving previous day's map (%s) — now shifted to %s", m["date"], _fmt(now_min))

    # A map cannot have been generated in the future. If it looks that way our
    # clock is behind the planner's, and every window would fire at the wrong
    # hour — so refuse rather than execute the plan at the wrong time of day.
    skew, stamp = newest_map_skew(_now)
    if skew is not None and skew > CLOCK_SKEW_LIMIT_MIN:
        log.error("clock skew: newest map generated %s but this host reads %s "
                  "(%.0f min behind). The plan's times are the planner's local time, so "
                  "every window would fire at the wrong hour — fix this host's timezone. "
                  "Refusing to dispatch.", stamp, _now.isoformat(timespec="seconds"), skew)
        sys.exit(1)

    state = load_state()
    try:
        live = read_live(cfg["api_url"])
    except Exception as e:
        log.error("cannot read inverter: %s", e); sys.exit(1)

    plan = decide(m, now_min, live, cfg, state.get("hold"))

    if args.show:
        show(m, now_min, live, plan); return

    log.info("run at %s  soc %d%%  surplus %+.0f W → %s",
             _fmt(now_min % DAY_MIN), live["soc"], surplus_w(live), plan["reason"])

    try:
        fw = _get(cfg["api_url"], "/api/settings")
    except Exception as e:
        log.error("cannot read settings: %s", e); sys.exit(1)

    fw_tou     = fw.get("tou") or {}
    fw_general = fw.get("general") or {}
    fw_battery = fw.get("battery")
    n_bat      = resolve_battery_count(cfg, fw_battery)

    # Closing comes first and opening comes last, so the gate is never open
    # over a schedule that has not been confirmed.
    hits = [ensure_tou_enabled(fw_tou, cfg["api_url"], args.dry_run)]
    if not plan["mode_on"]:
        hits.append(sync_mode(False, fw_general, cfg["api_url"], args.dry_run))

    wrote, verified = sync_slots(plan, fw_tou, cfg, cfg["api_url"], args.dry_run)
    hits.append(wrote)
    hits.append(sync_general(plan, fw_general, cfg["api_url"], args.dry_run))
    hits.append(sync_battery(plan, fw_battery, n_bat, cfg, cfg["api_url"], args.dry_run))

    if plan["mode_on"]:
        if verified:
            hits.append(sync_mode(True, fw_general, cfg["api_url"], args.dry_run))
        else:
            log.error("refusing to open export over a schedule that did not verify")
            hits.append(sync_mode(False, fw_general, cfg["api_url"], args.dry_run))

    if not any(hits):
        log.info("all up to date")

    if not args.dry_run:
        save_state({"date": date.today().isoformat(), "hold": plan["hold"]})


if __name__ == "__main__":
    main()
