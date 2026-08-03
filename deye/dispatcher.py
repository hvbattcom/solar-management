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

The plan is executed here, every five minutes, against two orthogonal controls:

    target vs SoC   may the battery be dispatched at all?
                    below the current level it discharges; above it, it is held
                    and the house falls back to the grid.

    sell bit        may what it dispatches reach the grid?

Which covers every state the plan asks for:

    carrying the house   target below SoC, sell off
    banking solar        target above SoC          (surplus charges the pack)
    selling the battery  target at the plan floor, sell on

Windows therefore cost nothing. Twelve of them are the same as two, resolved at
5-minute granularity, and a window landing next to a slot boundary — which used
to be a real hazard — no longer means anything.

All six slots are written with identical values, so crossing a boundary between
runs is a no-op. Within the block the firmware sees SOC (166-171) before the
control bits (172-177), which is the safe order: the target is always in place
before selling is enabled, and never lowered while selling is still on.

Register 142 (work mode) is pinned to Selling First and left alone; selling is
gated by the sell bit, not by the mode.

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

WORK_MODE = "Selling First"

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
    """Everything the inverter should be doing at this moment."""
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

    if win and price_ok:
        # Selling the battery: drain to the plan's floor for this window.
        floor  = max(lo, min(hi, int(win["soc_floor_pct"])))
        guard  = live["soc"] <= floor + SOC_GUARD_MARGIN
        if guard:
            log.warning("soc guard: %d%% is within %d%% of the floor %d%% — not selling",
                        live["soc"], SOC_GUARD_MARGIN, floor)
        return {"target": floor, "sell": not guard, "hold": False,
                "ceiling": ceiling, "solar_sell": price_ok,
                "charge_a": charge_a, "reason": "window" if not guard else "window (guarded)"}

    # Outside a window the battery is never sold. Whether it is held or put on
    # the house is decided by the live power balance, not the clock.
    hold = hold_pack(live, previous_hold)
    if charge_a is not None and not hold:
        charge_a = max(charge_a, full_charge_amps(events, charge_a))
    return {"target": hi if hold else lo, "sell": False, "hold": hold,
            "ceiling": ceiling, "solar_sell": price_ok,
            "charge_a": charge_a,
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


def sync_slots(plan: dict, fw_tou: dict, opts: dict, api_url: str, dry_run: bool) -> bool:
    """Write the same values into all six slots, plus the fixed times.

    Identical slots mean a boundary crossing between runs changes nothing, so
    the dispatcher's 5-minute cadence is the only granularity that matters."""
    fw   = {s["slot"]: s for s in fw_tou.get("slots", [])}
    want = [{"slot": n + 1, "time": SCAFFOLD_TIMES[n],
             "power_w": opts["inverter_power_w"],
             "soc_pct": plan["target"], "sell": plan["sell"],
             "grid_charge": False, "gen_charge": False}
            for n in range(TOU_SLOT_COUNT)]

    def differs(w):
        f = fw.get(w["slot"], {})
        return (f.get("time") != w["time"] or f.get("soc_pct") != w["soc_pct"]
                or bool(f.get("sell")) != w["sell"]
                or bool(f.get("grid_charge")) or bool(f.get("gen_charge"))
                or round(f.get("power_w", -1)) != round(w["power_w"]))

    if not any(differs(w) for w in want):
        log.debug("slots: up to date (target %d%%, sell %s)", plan["target"], plan["sell"])
        return False
    log.info("slots → target %d%%  sell %s  (%s)", plan["target"], plan["sell"], plan["reason"])
    _post(api_url, "/api/settings/tou/all", want, dry_run)
    return True


def sync_general(plan: dict, fw_general: dict, api_url: str, dry_run: bool) -> bool:
    body = {}
    if fw_general.get("limit_control") != WORK_MODE:
        # Pinned, not toggled: selling is gated by the sell bit.
        body["limit_control"] = WORK_MODE
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

    hits = [
        ensure_tou_enabled(fw_tou, cfg["api_url"], args.dry_run),
        sync_slots(plan, fw_tou, cfg, cfg["api_url"], args.dry_run),
        sync_general(plan, fw_general, cfg["api_url"], args.dry_run),
        sync_battery(plan, fw_battery, n_bat, cfg, cfg["api_url"], args.dry_run),
    ]
    if not any(hits):
        log.info("all up to date")

    if not args.dry_run:
        save_state({"date": date.today().isoformat(), "hold": plan["hold"]})


if __name__ == "__main__":
    main()
