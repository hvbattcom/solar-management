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


# ── 1. Surplus is measured from the power balance, not the clock ─────────────
# surplus = -(battery + grid): what is charging the pack plus what is leaving.
assert d.surplus_w(live(batt=-1300, grid=0)) == 1300, "charging 1.3 kW is 1.3 kW of surplus"
assert d.surplus_w(live(batt=0, grid=-2000)) == 2000, "exporting counts as surplus"
assert d.surplus_w(live(batt=0, grid=200)) == -200, "importing is a deficit"
assert d.surplus_w(live(batt=24110, grid=-26627)) == 2517  # the drain, mid-incident

# ── 2. Hold/release has hysteresis so it cannot flap ─────────────────────────
assert d.hold_pack(live(batt=-1300), previous=None) is True
assert d.hold_pack(live(grid=500), previous=True) is False, "importing releases the pack"
# Between the thresholds the previous answer stands.
mid = live(batt=-100)          # surplus 100 W, between RELEASE_BELOW_W and HOLD_ABOVE_W
assert 0 < d.surplus_w(mid) < d.HOLD_ABOVE_W
assert d.hold_pack(mid, previous=True)  is True
assert d.hold_pack(mid, previous=False) is False
assert d.hold_pack(mid, previous=None)  is False, "no history defaults to the house"

# ── 3. Outside a window the battery is never sold ────────────────────────────
plan = d.decide(m(events=EXPORT_ON), d._tm("12:00"), live(soc=80, batt=-2000), OPTS, None)
assert plan["sell"] is False, "no window means no battery selling, whatever the price"
assert plan["hold"] is True and plan["target"] == 100, "surplus banks into the pack"
assert plan["solar_sell"] is True, "solar sell still follows the price"

plan = d.decide(m(events=EXPORT_ON), d._tm("23:00"), live(soc=80, grid=400), OPTS, True)
assert plan["sell"] is False
assert plan["hold"] is False and plan["target"] == 15, \
    "drawing from the grid must release the pack to the house"

# A good price at night must not sell the pack — that was the original incident.
plan = d.decide(m(events=EXPORT_ON), d._tm("03:00"), live(soc=95, grid=300), OPTS, None)
assert plan["sell"] is False and plan["target"] == 15

# ── 4. Inside a window the battery sells to the plan's floor ─────────────────
win = m(windows=[("20:00", "22:00", 17)], events=EXPORT_ON)
plan = d.decide(win, d._tm("21:00"), live(soc=80), OPTS, None)
assert plan["sell"] is True and plan["target"] == 17 and plan["hold"] is False

# ...but only while the price says so.
off = m(windows=[("20:00", "22:00", 17)], events=EXPORT_OFF)
plan = d.decide(off, d._tm("21:00"), live(soc=80), OPTS, None)
assert plan["sell"] is False, "a window with the price below threshold sells nothing"

# Outside its hours the same window is inert.
plan = d.decide(win, d._tm("19:00"), live(soc=80, batt=-500), OPTS, None)
assert plan["sell"] is False

# ── 5. The SoC guard stops a drain at the floor ──────────────────────────────
plan = d.decide(win, d._tm("21:00"), live(soc=18), OPTS, None)
assert plan["sell"] is False, "18% is within the margin of a 17% floor"
plan = d.decide(win, d._tm("21:00"), live(soc=20), OPTS, None)
assert plan["sell"] is True, "20% is clear of it"

# ── 6. Floors are clamped into the planner's envelope ────────────────────────
deep = m(windows=[("20:00", "22:00", 5)], events=EXPORT_ON, lo=15, hi=90)
assert d.decide(deep, d._tm("21:00"), live(soc=80), OPTS, None)["target"] == 15, \
    "a floor under batt_min must clamp up to it"
assert d.decide(m(events=EXPORT_ON, lo=15, hi=90), d._tm("12:00"),
                live(batt=-2000), OPTS, None)["target"] == 90, "holding uses soft max"

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
assert d.decide(many, d._tm("08:15"), live(soc=80), OPTS, None)["sell"] is True
assert d.decide(many, d._tm("08:45"), live(soc=80, batt=-900), OPTS, None)["sell"] is False

# ── 8. Charge current: restored outside windows, split, capped ───────────────
ev = [{"time": "00:00", "export": True, "charge_amps": 40},
      {"time": "10:00", "charge_amps": 1}]          # throttled because it expects to sell
plan = d.decide(m(events=ev), d._tm("11:00"), live(soc=80, grid=400), OPTS, None)
assert plan["charge_a"] == 40, "not selling here, so the throttle would only waste solar"
plan = d.decide(m(windows=[("10:00", "12:00", 17)], events=ev),
                d._tm("11:00"), live(soc=80), OPTS, None)
assert plan["charge_a"] == 1, "inside a window the plan's throttle stands"

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
                live(), OPTS, None)["ceiling"] == 27000
capped = dict(OPTS, max_sell_power_w=20000)
assert d.decide(m(events=EXPORT_ON, sell_kw=27.0), d._tm("12:00"),
                live(), capped, None)["ceiling"] == 20000, "the plant limit wins"
assert d.decide(m(events=EXPORT_ON, sell_kw=10.0), d._tm("12:00"),
                live(), capped, None)["ceiling"] == 10000, "and never raises a lower plan"

# ── 10. All six slots are written identically, on fixed times ───────────────
posted = []
d._post = lambda url, path, body, dry: posted.append((path, body))
plan = d.decide(win, d._tm("21:00"), live(soc=80), OPTS, None)
empty_fw = {"slots": [{"slot": n, "time": "00:00", "soc_pct": 0, "sell": False,
                       "grid_charge": True, "gen_charge": False, "power_w": 0}
                      for n in range(1, 7)]}
assert d.sync_slots(plan, empty_fw, OPTS, "http://x", False) is True
path, body = posted[-1]
assert path == "/api/settings/tou/all" and len(body) == 6
assert [s["time"] for s in body] == d.SCAFFOLD_TIMES, "times are fixed scaffolding"
assert len({s["soc_pct"] for s in body}) == 1, "identical, so a boundary crossing is a no-op"
assert len({s["sell"] for s in body}) == 1
assert all(s["grid_charge"] is False and s["gen_charge"] is False for s in body), \
    "grid charge would flip the SOC field into a charge target"

# Nothing to do when the firmware already matches.
settled = {"slots": [dict(s) for s in body]}
posted.clear()
assert d.sync_slots(plan, settled, OPTS, "http://x", False) is False, posted

# ── 11. The work mode is pinned, never toggled ──────────────────────────────
posted.clear()
gen_ok = {"limit_control": "Selling First", "solar_sell_enable": True,
          "max_sell_power_w": 27000}
assert d.sync_general(dict(plan, ceiling=27000, solar_sell=True), gen_ok,
                      "http://x", False) is False
assert d.sync_general(dict(plan, ceiling=27000, solar_sell=True),
                      dict(gen_ok, limit_control="Zero Export to CT"),
                      "http://x", False) is True
assert posted[-1][1] == {"limit_control": "Selling First"}, posted[-1]
# Solar sell tracks the price on its own.
posted.clear()
assert d.sync_general(dict(plan, ceiling=27000, solar_sell=False), gen_ok,
                      "http://x", False) is True
assert posted[-1][1] == {"solar_sell_enable": False}

print("all deye dispatcher tests passed")
