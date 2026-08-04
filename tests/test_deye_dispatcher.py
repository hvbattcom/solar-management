"""Offline tests for deye/dispatcher.py — no inverter, no network.

Run directly:  python3 tests/test_deye_dispatcher.py
"""
import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("deye_dispatcher", _ROOT / "deye" / "dispatcher.py")
d     = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(d)

OPTS = {"inverter_power_w": 30000, "battery_count": None,
        "max_charge_amps": 0, "max_sell_power_w": 0}


def live(soc=80, batt=0, grid=0, pv=0):
    """battery: + discharging.  grid: + importing."""
    return {"soc": soc, "batt_w": batt, "grid_w": grid, "pv_w": pv}


def m(windows=(), events=(), lo=15, hi=100, sell_kw=27.0):
    return {"tou_slots": [{"start": s, "end": e, "soc_floor_pct": f, "amps": 40}
                          for s, e, f in windows],
            "events": list(events), "soc_min_pct": lo, "soc_max_pct": hi,
            "sell_kw": sell_kw}


EXPORT_ON  = [{"time": "00:00", "export": True,  "charge_amps": 40}]
EXPORT_OFF = [{"time": "00:00", "export": False, "charge_amps": 40}]


SUN = 6000          # PV that counts as daylight
DARK = 0

# ── 1. Carrying the house: nothing sold, pack on the load ───────────────────
# power 0 is what stops the battery being sold; the mode stays Selling First
# throughout, so power is the only thing standing between plan and grid.
ev_bank = [{"time": "00:00", "export": False, "charge_amps": 40}]
plan = d.decide(m(events=ev_bank), d._tm("12:00"), live(soc=80, pv=SUN), OPTS)
assert plan["power"] == 0, "no sale means no slot power"
assert plan["target"] == 15, "batt_min, so the pack runs the load"
assert plan["charge_a"] == 40, "and banks the surplus"
assert plan["reason"] == "carrying the house"
assert plan["mode"] == d.MODE_CLOSED, \
    "banking with the gate open sells the PV and puts the house on the grid"

# ── 2. Selling solar: power 0, target ABOVE SoC, charge throttled ───────────
ev_solar = [{"time": "00:00", "export": True, "charge_amps": 1}]
plan = d.decide(m(events=ev_solar), d._tm("12:00"), live(soc=80, pv=SUN), OPTS)
assert plan["reason"] == "selling solar surplus"
assert plan["mode"] == d.MODE_SELL, "the gate has to be open to sell anything"
assert plan["power"] == 0, "the pack must not be sold"
assert plan["target"] == 100, "above SoC, so it cannot feed the sale either"
assert plan["charge_a"] == 1, "and cannot absorb the surplus"

# After dark the same signals must NOT raise the target — that is the grid
# trickle this whole model replaced.
plan = d.decide(m(events=ev_solar), d._tm("23:00"), live(soc=80, pv=DARK), OPTS)
assert plan["target"] == 15, "no sun, so the pack stays on the house"
assert plan["reason"] == "carrying the house"

# Price below threshold is not a solar sale either, however bright it is.
plan = d.decide(m(events=ev_bank), d._tm("12:00"), live(soc=80, pv=SUN), OPTS)
assert plan["target"] == 15 and plan["power"] == 0

# ── 2b. The sell bit rides with the slot power ──────────────────────────────
# Measured: 30 kW of slot power against a target 8 points below SoC sold nothing
# until the bit went on, then 16 kW flowed within seconds. So the bit is required
# even in Selling First, and clearing it is what keeps the pack out of a solar
# sale.
_solar = d.decide(m(events=ev_solar), d._tm("12:00"), live(soc=80, pv=SUN), OPTS)
assert d.desired_slots(_solar, OPTS)[0]["sell"] is False, "solar sale must not arm the bit"
_house = d.decide(m(events=ev_bank), d._tm("12:00"), live(soc=80, pv=SUN), OPTS)
assert d.desired_slots(_house, OPTS)[0]["sell"] is False

# ── 3. Selling the battery: high power, low target, both from the map ───────
win = m(windows=[("20:00", "22:00", 17)], events=ev_solar, sell_kw=27.0)
plan = d.decide(win, d._tm("21:00"), live(soc=80, pv=DARK), OPTS)
assert plan["reason"] == "window: selling the battery"
assert plan["mode"] == d.MODE_SELL
assert plan["power"] == 27000, "the map's sell power"
assert plan["target"] == 17, "and the window's own floor"
assert all(s["sell"] is True for s in d.desired_slots(plan, OPTS)), \
    "a battery sale arms the bit on every slot"

off = m(windows=[("20:00", "22:00", 17)], events=ev_bank)
assert d.decide(off, d._tm("21:00"), live(soc=80), OPTS)["power"] == 0, \
    "a window under the price threshold sells nothing"
assert d.decide(win, d._tm("19:00"), live(soc=80, pv=DARK), OPTS)["power"] == 0, \
    "outside its hours the window is inert"

# ── 4. The SoC guard is a backstop, not an early stop ───────────────────────
# The planner's tou_floor_margin is set so a window runs out of time before it
# runs out of charge; withholding above the floor would eat that margin.
assert d.decide(win, d._tm("21:00"), live(soc=18, pv=DARK), OPTS)["power"] == 27000, \
    "one point above the floor must still sell"
assert d.decide(win, d._tm("21:00"), live(soc=17, pv=DARK), OPTS)["power"] == 0, \
    "at the floor it stops"
assert d.decide(win, d._tm("21:00"), live(soc=10, pv=DARK), OPTS)["power"] == 0

# ── 5. Sell power comes from the map, capped by the plant limit ─────────────
capped = dict(OPTS, max_sell_power_w=20000)
assert d.decide(win, d._tm("21:00"), live(soc=80), capped)["power"] == 20000
w10 = m(windows=[("20:00", "22:00", 17)], events=ev_solar, sell_kw=10.0)
assert d.decide(w10, d._tm("21:00"), live(soc=80), capped)["power"] == 10000, \
    "a cap never raises a lower plan"

# ── 6. Floors are clamped into the planner's envelope ────────────────────────
deep = m(windows=[("20:00", "22:00", 5)], events=EXPORT_ON, lo=15, hi=90)
assert d.decide(deep, d._tm("21:00"), live(soc=80), OPTS)["target"] == 15, \
    "a floor under batt_min must clamp up to it"
high = m(windows=[("20:00", "22:00", 95)], events=EXPORT_ON, lo=15, hi=90)
assert d.decide(high, d._tm("21:00"), live(soc=80), OPTS)["target"] == 90, \
    "and a floor above soft max must clamp down to it"
assert d.decide(m(events=ev_solar, lo=15, hi=90), d._tm("12:00"),
                live(soc=50, pv=SUN), OPTS)["target"] == 90, \
    "a solar sale raises the target to soft max, not a hardcoded 100"

# ── 7. Windows wrap past midnight ────────────────────────────────────────────
tail = m(windows=[("24:15", "25:00", 17)], events=EXPORT_ON)
assert d.active_window(tail["tou_slots"], d._tm("00:30")) is not None, \
    "'24:15-25:00' is 00:15-01:00 on the daily cycle"
assert d.active_window(tail["tou_slots"], d._tm("12:00")) is None
straddle = m(windows=[("23:30", "24:30", 17)], events=EXPORT_ON)
for t in ("23:45", "00:15"):
    assert d.active_window(straddle["tou_slots"], d._tm(t)) is not None, t
assert d.active_window(straddle["tou_slots"], d._tm("12:00")) is None

# Any number of windows costs the same — there are no boundaries to run out of.
many = m(windows=[(f"{h:02d}:00", f"{h:02d}:30", 17) for h in range(0, 24, 2)],
         events=EXPORT_ON)
assert len(many["tou_slots"]) == 12
assert d.decide(many, d._tm("08:15"), live(soc=80), OPTS)["power"] > 0
assert d.decide(many, d._tm("08:45"), live(soc=80, pv=DARK), OPTS)["power"] == 0

# ── 8. Charge current: restored outside windows, split, capped ───────────────
ev = [{"time": "00:00", "export": True, "charge_amps": 40},
      {"time": "10:00", "charge_amps": 1}]          # throttled because it expects to sell
assert d._per_bat(40, 1) == 40 and d._per_bat(40, 2) == 20
assert d._per_bat(80, 1, 40) == 40, "a cap must protect a smaller pack"
assert d._per_bat(30, 1, 40) == 30, "and never raise a smaller request"
assert d._per_bat(1, 2) == 1, "never below 1 A"
zona = {"max_charge_a": 40, "bat2_max_charge_a": 0}
assert d.resolve_battery_count(OPTS, zona) == 1, "0 A means no second battery"
assert d.resolve_battery_count(OPTS, {"bat2_max_charge_a": 50}) == 2
assert d.resolve_battery_count({"battery_count": 2}, zona) == 2

# ── 9. The sell ceiling follows the plan, capped by the plant limit ──────────
assert d.decide(m(events=EXPORT_ON, sell_kw=27.0), d._tm("12:00"),
                live(), OPTS)["ceiling"] == 27000
capped = dict(OPTS, max_sell_power_w=20000)
assert d.decide(m(events=EXPORT_ON, sell_kw=27.0), d._tm("12:00"),
                live(), capped)["ceiling"] == 20000, "the plant limit wins"
assert d.decide(m(events=EXPORT_ON, sell_kw=10.0), d._tm("12:00"),
                live(), capped)["ceiling"] == 10000, "and never raises a lower plan"

# ── 10. All six slots identical, on fixed times, and verified after writing ──
posted = []
fake_fw = {"tou": {"slots": [{"slot": n, "time": "00:00", "soc_pct": 0, "sell": False,
                              "grid_charge": True, "gen_charge": False, "power_w": 0}
                             for n in range(1, 7)]}}
d._post = lambda url, path, body, dry: posted.append((path, body))
d._get  = lambda url, path, attempts=3: fake_fw

plan = d.decide(win, d._tm("21:00"), live(soc=80), OPTS)

# A write that never lands must report itself as unverified, not silently pass.
wrote, verified = d.sync_slots(plan, fake_fw["tou"], OPTS, "http://x", False)
assert wrote is True and verified is False, "an unconfirmed write must not look successful"
assert len(posted) == 2, f"should retry once, posted {len(posted)}"

# Now let the read-back reflect what was written.
posted.clear()
def _post_and_apply(url, path, body, dry):
    posted.append((path, body))
    if path == "/api/settings/tou/all":
        fake_fw["tou"] = {"slots": [dict(s) for s in body]}
d._post = _post_and_apply
wrote, verified = d.sync_slots(plan, {"slots": []}, OPTS, "http://x", False)
assert wrote is True and verified is True
path, body = posted[-1]
assert path == "/api/settings/tou/all" and len(body) == 6
assert [s["time"] for s in body] == d.SCAFFOLD_TIMES, "times are fixed scaffolding"
assert len({s["soc_pct"] for s in body}) == 1, "identical, so a boundary crossing is a no-op"
assert all(s["grid_charge"] is False and s["gen_charge"] is False for s in body), \
    "grid charge would flip the SOC field into a charge target"

# Nothing to do when the firmware already matches.
posted.clear()
assert d.sync_slots(plan, fake_fw["tou"], OPTS, "http://x", False) == (False, True), posted

# ── 11. The work mode is a pinned invariant, never a control ────────────────
posted.clear()
assert d.sync_mode(d.MODE_SELL, {"limit_control": d.MODE_SELL}, "http://x", False) is False, \
    "already there: nothing to write"
assert d.sync_mode(d.MODE_CLOSED, {"limit_control": d.MODE_SELL}, "http://x", False) is True
assert posted[-1][1] == {"limit_control": "Zero Export to CT"}
assert d.sync_mode(d.MODE_SELL, {"limit_control": d.MODE_CLOSED}, "http://x", False) is True
assert posted[-1][1] == {"limit_control": "Selling First"}

# Every non-selling state must shut the gate, whatever the price says.
for _ev in (ev_bank, [{"time": "00:00", "export": True, "charge_amps": 38}]):
    for _pv in (0, SUN):
        _p = d.decide(m(events=_ev), d._tm("14:16"), live(soc=32, pv=_pv), OPTS)
        assert _p["mode"] == d.MODE_CLOSED, (_ev, _pv, _p)

# Solar sell and the ceiling never touch the mode.
posted.clear()
gen_ok = {"solar_sell_enable": True, "max_sell_power_w": 27000}
assert d.sync_general(dict(plan, ceiling=27000, solar_sell=True), gen_ok, "http://x", False) is False
assert d.sync_general(dict(plan, ceiling=27000, solar_sell=False), gen_ok, "http://x", False) is True
assert posted[-1][1] == {"solar_sell_enable": False}
assert all("limit_control" not in b for _, b in posted[1:]), "the mode is pin_mode's alone"

# ── 12. Clock skew is caught against the NEWEST map ─────────────────────────
# The map's times are the planner's local time. A host in a different timezone
# runs every window at the wrong hour: a UTC plant executed a 21:00 window at
# 00:24 local and would have sold for three more hours.
import json as _json, os as _os, pathlib as _pl, tempfile as _tf
from datetime import datetime as _dt

assert d.clock_skew_min({"generated_at": "2026-08-04T00:24:00"},
                        _dt(2026, 8, 3, 21, 24)) == 180
assert abs(d.clock_skew_min({"generated_at": "2026-08-04T00:24:00"},
                            _dt(2026, 8, 4, 0, 26))) <= 2, "aligned clocks pass"
assert d.clock_skew_min({}, _dt.now()) is None
assert d.clock_skew_min({"generated_at": "not a date"}, _dt.now()) is None

with _tf.TemporaryDirectory() as tmp:
    d._MAPS_DIR = _pl.Path(tmp)
    # Yesterday's map, written first, is the one a behind-the-planner host picks.
    old = d._MAPS_DIR / "map_2026-08-03_X.json"
    old.write_text(_json.dumps({"generated_at": "2026-08-03T19:59:00"}))
    _os.utime(old, (1, 1))
    new = d._MAPS_DIR / "map_2026-08-04_X.json"
    new.write_text(_json.dumps({"generated_at": "2026-08-04T00:24:48"}))
    skew, stamp = d.newest_map_skew(_dt(2026, 8, 3, 21, 34))
    assert 170 <= skew <= 171, skew
    assert stamp == "2026-08-04T00:24:48", "must measure the newest map, not the selected one"
    assert skew > d.CLOCK_SKEW_LIMIT_MIN, "and that must trip the guard"
    # The stale map alone would have hidden it entirely.
    assert d.clock_skew_min(_json.loads(old.read_text()), _dt(2026, 8, 3, 21, 34)) < 0

print("all deye dispatcher tests passed")
