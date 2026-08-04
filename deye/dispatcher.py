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

The plan is executed here, every five minutes, as one of three states. Each
names its own work mode, slot power, target and charge current — and the mode
matters as much as the rest: banking with the gate left open makes the plant
sell the PV it was told to store and puts the house on the grid.

    carrying the house   mode SHUT, power 0, target batt_min, charge full.
                         The pack runs the load and the sun charges it.
    selling solar        mode open, power 0, target ABOVE SoC, charge ~1 A, so
                         the pack can neither absorb the surplus nor feed the
                         sale and only PV leaves.
    selling the battery  mode open, power = the plan's sell power, target = the
                         window's floor, sell bit armed.

The sell bit rides with the slot power. Measured: 30 kW of slot power against a
target below SoC sells nothing until the bit goes on, then 16 kW flows within
seconds — so the bit is required even in Selling First, and clearing it is what
keeps the pack out of a solar-only sale. Every figure comes from the map.

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
_LOG_FILE    = Path(__file__).resolve().parent / "dispatcher.log"
_DEFAULT_CFG = Path(__file__).resolve().parent / "config.cfg"

EVENT_TOLERANCE_MIN = 3
TOU_SLOT_COUNT      = 6
DAY_MIN             = 1440

# Fixed scaffolding. The values are arbitrary — every slot carries identical
# settings, so where the boundaries fall changes nothing. They exist only
# because the hardware insists on six of them.
SCAFFOLD_TIMES = ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"]

# The work mode IS a control, one per state. Banking solar needs the gate shut:
# left at Selling First the plant sells the PV instead of storing it, and the
# house — which that PV would have covered — falls back to the grid. That is the
# few-hundred-watt draw that kept reappearing. Measured on the plant: same state,
# flip 142 to Zero Export to CT, and the draw goes.
MODE_SELL   = "Selling First"
MODE_CLOSED = "Zero Export to CT"

# A battery sale needs
# three things together — the sell bit, a slot power above zero, and a target
# below SoC — and drops any one of them to stop. Measured on the plant: power at
# 30 kW with a target 8 points under SoC sold nothing until the bit went on, and
# then 16 kW flowed within seconds.
#
# Earlier readings of this were confounded because the slot power was held high
# in all of them, so it never appeared as the variable and the mode looked like
# the cause.
# Selling surplus solar means giving it nowhere else to go: the charge current
# drops to ~1 A so the pack cannot absorb it, and the target goes ABOVE SoC so
# the pack will not discharge into the sale either. Only PV is left to leave.
# The plan already signals this by dropping charge_amps when it expects to sell.
SOLAR_SELL_CHARGE_A = 2

# ...but only while the sun is actually up. A high target at night would hold the
# pack off the house and put the plant back on the grid trickle this replaced.
SOLAR_SELL_MIN_PV_W = 500

# The map's times are the PLANNER's local time, and the dispatcher compares them
# against its own clock. A host on a different timezone executes the whole plan
# by that offset — a UTC plant ran a 21:00 window at 00:24 local and would have
# kept selling for three more hours.
CLOCK_SKEW_LIMIT_MIN = 90

# A true backstop only. The inverter already stops at the slot target, and the
# planner's tou_floor_margin is set so a window runs out of TIME before it runs
# out of charge — withholding early would eat exactly that margin.
SOC_GUARD_AT_FLOOR = True
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

def decide(m: dict, now_min: int, live: dict, opts: dict) -> dict:
    """Everything the inverter should be doing at this moment.

    The work mode stays at Selling First throughout. What leaves the plant is
    decided by the slot power — the battery's sell rate — together with the
    target, which says whether the pack may be drawn on at all:

      carrying the house   power 0, target batt_min. The pack runs the load and
                           surplus solar charges it. Nothing is sold.
      selling solar        power 0, target ABOVE SoC, charge throttled to ~1 A.
                           The pack can neither absorb the surplus nor feed the
                           sale, so only PV leaves.
      selling the battery  power = the plan's sell power, target = the window's
                           floor, so the pack drains into the grid down to it.

    Every figure comes from the map; none of it is decided here."""
    events   = m.get("events", [])
    want     = desired_state(events, now_min)
    price_ok = bool(want.get("export"))
    lo, hi   = soc_envelope(m)
    win      = active_window(m.get("tou_slots", []), now_min)
    charge_a = want.get("charge_amps")

    sell_power = int(float(m.get("sell_kw", 0)) * 1000)
    if sell_power <= 0:
        sell_power = opts["inverter_power_w"]
    if opts["max_sell_power_w"] > 0:
        sell_power = min(sell_power, opts["max_sell_power_w"])

    if win and price_ok:
        floor = max(lo, min(hi, int(win["soc_floor_pct"])))
        if SOC_GUARD_AT_FLOOR and live["soc"] <= floor:
            log.warning("soc guard: %d%% is at or below the floor %d%% — not selling",
                        live["soc"], floor)
            # The pack is spent, but the sun may still be worth selling.
            return {"target": floor, "power": 0, "ceiling": sell_power,
                    "mode": MODE_SELL, "solar_sell": price_ok, "charge_a": charge_a,
                    "reason": "window (guarded at the floor)"}
        return {"target": floor, "power": sell_power, "ceiling": sell_power,
                "mode": MODE_SELL, "solar_sell": price_ok, "charge_a": charge_a,
                "reason": "window: selling the battery"}

    # The plan signals that it expects to sell solar by dropping charge_amps;
    # requiring real PV as well keeps a high target off the pack after dark.
    selling_solar = (price_ok
                     and charge_a is not None and charge_a <= SOLAR_SELL_CHARGE_A
                     and live["pv_w"] >= SOLAR_SELL_MIN_PV_W)
    if selling_solar:
        return {"target": hi, "power": 0, "ceiling": sell_power,
                "mode": MODE_SELL, "solar_sell": True, "charge_a": charge_a,
                "reason": "selling solar surplus"}

    # Banking: the gate must be SHUT, or the plant sells the PV it was told to
    # store and puts the house on the grid.
    return {"target": lo, "power": 0, "ceiling": sell_power,
            "mode": MODE_CLOSED, "solar_sell": price_ok, "charge_a": charge_a,
            "reason": "carrying the house"}


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
             "power_w": plan["power"],
             "soc_pct": plan["target"], "sell": plan["power"] > 0,
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
        log.debug("slots: up to date (target %d%%, power %d W)", plan["target"], plan["power"])
        return False, True

    want = desired_slots(plan, opts)
    log.info("slots → target %d%%  power %d W  (%s)", plan["target"], plan["power"], plan["reason"])
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


def sync_mode(want: str, fw_general: dict, api_url: str, dry_run: bool) -> bool:
    """Written on its own so the caller controls the ordering: shut before the
    slots change, opened only after they have been read back and confirmed."""
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
          f"batt {live['batt_w']:+.0f} W   grid {live['grid_w']:+.0f} W")
    print(f"\nNow {_fmt(now_min % DAY_MIN)} → {plan['reason']}")
    print(f"  mode        {plan['mode']}")
    print(f"  target      {plan['target']}%")
    print(f"  slot power  {plan['power']} W   (sell ceiling {plan['ceiling']} W)")
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

    try:
        live = read_live(cfg["api_url"])
    except Exception as e:
        log.error("cannot read inverter: %s", e); sys.exit(1)

    plan = decide(m, now_min, live, cfg)

    if args.show:
        show(m, now_min, live, plan); return

    log.info("run at %s  soc %d%% → %s", _fmt(now_min % DAY_MIN), live["soc"], plan["reason"])

    try:
        fw = _get(cfg["api_url"], "/api/settings")
    except Exception as e:
        log.error("cannot read settings: %s", e); sys.exit(1)

    fw_tou     = fw.get("tou") or {}
    fw_general = fw.get("general") or {}
    fw_battery = fw.get("battery")
    n_bat      = resolve_battery_count(cfg, fw_battery)

    # Shutting the gate comes first and opening it comes last, so export is
    # never enabled over a slot block that has not been confirmed.
    hits = [ensure_tou_enabled(fw_tou, cfg["api_url"], args.dry_run)]
    if plan["mode"] == MODE_CLOSED:
        hits.append(sync_mode(MODE_CLOSED, fw_general, cfg["api_url"], args.dry_run))

    wrote, verified = sync_slots(plan, fw_tou, cfg, cfg["api_url"], args.dry_run)
    hits.append(wrote)
    hits.append(sync_general(plan, fw_general, cfg["api_url"], args.dry_run))
    hits.append(sync_battery(plan, fw_battery, n_bat, cfg, cfg["api_url"], args.dry_run))

    if plan["mode"] != MODE_CLOSED:
        if verified:
            hits.append(sync_mode(plan["mode"], fw_general, cfg["api_url"], args.dry_run))
        else:
            log.error("refusing to open export over a slot block that did not verify")
            hits.append(sync_mode(MODE_CLOSED, fw_general, cfg["api_url"], args.dry_run))

    if not any(hits):
        log.info("all up to date")




if __name__ == "__main__":
    main()
