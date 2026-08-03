"""Offline tests for deye/dispatcher.py — no inverter, no network.

Run directly:  python3 tests/test_deye_dispatcher.py
"""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("deye_dispatcher", _ROOT / "deye" / "dispatcher.py")
d     = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(d)

OPTS = {"inverter_power_w": 30000, "write_sell_bit": True, "battery_count": 2,
        "max_sell_power_w": 0, "sell_solar_outside_windows": True}

FLOOR, SOC_HI, SELL_W = 15, 100, 30000


def build(tou, now="12:00", opts=OPTS, lo=FLOOR, hi=SOC_HI, events=None):
    return d.build_desired_slots(tou, d._tm(now), opts, lo, hi, SELL_W, events)


def w(start, end, floor=20, amps=100):
    return {"start": start, "end": end, "soc_floor_pct": floor, "amps": amps}


def coverage(slots):
    """Reconstruct the 24 h behaviour the firmware would derive from the slots."""
    out = []
    for i, s in enumerate(slots):
        nxt = slots[(i + 1) % len(slots)]
        out.append((d._tm(s["time"]), d._tm(nxt["time"]), bool(s.get("sell")), s["soc_pct"]))
    return out


def selling_minutes(slots):
    mins = set()
    for start, end, sell, _ in coverage(slots):
        if not sell:
            continue
        rng = range(start, end) if start < end else list(range(start, 1440)) + list(range(0, end))
        mins |= set(rng)
    return mins


def check_invariants(slots, label):
    assert len(slots) == d.TOU_SLOT_COUNT, f"{label}: expected 6 slots, got {len(slots)}"
    times = [d._tm(s["time"]) for s in slots]
    assert times == sorted(times), f"{label}: times not ascending: {times}"
    assert len(set(times)) == len(times), f"{label}: duplicate times: {times}"
    # The firmware snaps times to a 5-minute grid; anything off-grid reads back
    # different from what was written and is rewritten on every single run.
    off = [t for t in times if t % d.TOU_TIME_GRID_MIN]
    assert not off, f"{label}: times off the firmware grid: {off}"
    assert [s["slot"] for s in slots] == [1, 2, 3, 4, 5, 6], f"{label}: slot numbering"
    for s in slots:
        assert s["grid_charge"] is False and s["gen_charge"] is False, \
            f"{label}: slot {s['slot']} must not charge (SOC would flip to a target)"
        assert 0 <= s["soc_pct"] <= 100, f"{label}: soc out of range"


# ── 1. No windows: an all-idle day still fills all six registers ──────────────
slots = build([])
check_invariants(slots, "empty plan")
assert not any(s.get("sell") for s in slots), "empty plan must not sell"
assert all(s["soc_pct"] == FLOOR for s in slots), "empty plan uses the idle floor"
assert all(s["power_w"] == 30000 for s in slots), "idle slots get the inverter ceiling"

# ── 2. The real map's shape: 3 windows, one running to midnight ──────────────
real = [w("05:45", "06:00", 15), w("20:00", "22:00", 23), w("22:45", "24:00", 23)]
slots = build(real, now="12:00")
check_invariants(slots, "3-window plan")
assert [s["time"] for s in slots] == ["00:00", "05:45", "06:00", "20:00", "22:00", "22:45"], \
    f"unexpected boundaries: {[s['time'] for s in slots]}"
assert [bool(s.get("sell")) for s in slots] == [False, True, False, True, False, True]
# The window that runs to 24:00 needs no closing boundary — slot 6 wraps to slot 1.
assert selling_minutes(slots) == (set(range(345, 360)) | set(range(1200, 1320))
                                  | set(range(1365, 1440))), "wrong selling coverage"
assert slots[1]["soc_pct"] == 15 and slots[3]["soc_pct"] == 23
assert slots[0]["soc_pct"] == FLOOR and slots[2]["soc_pct"] == FLOOR

# ── 3. Padding is a behavioural no-op ────────────────────────────────────────
one = [w("20:00", "22:00", 23)]
slots = build(one)
check_invariants(slots, "1-window plan")
assert selling_minutes(slots) == set(range(1200, 1320)), "padding changed behaviour"

# ── 4. Overflow: past windows are shed before future ones are deferred ───────
many = [w("01:00", "02:00"), w("03:00", "04:00"), w("05:00", "06:00"),
        w("20:00", "21:00"), w("22:00", "23:00")]
slots = build(many, now="12:00")          # first three already finished
check_invariants(slots, "overflow midday")
assert selling_minutes(slots) == set(range(1200, 1260)) | set(range(1320, 1380)), \
    "midday overflow should keep exactly the two future windows"

slots = build(many, now="00:30")          # nothing past yet — defer the latest
check_invariants(slots, "overflow at 00:30")
kept = selling_minutes(slots)
assert set(range(60, 120)) <= kept, "earliest window must survive deferral"
assert not (set(range(1320, 1380)) & kept), "latest window must be deferred"

# ── 5. Post-midnight tail wraps onto the cyclic schedule ─────────────────────
tail = [w("22:00", "23:00", 23), w("24:15", "25:00", 15)]
slots = build(tail, now="12:00")
check_invariants(slots, "post-midnight tail")
assert selling_minutes(slots) == set(range(15, 60)) | set(range(1320, 1380)), \
    "'24:15-25:00' must land at 00:15-01:00"
assert slots[0]["time"] == "00:00"

# A window straddling midnight splits across the wrap.
straddle = [w("23:30", "24:30", 23)]
slots = build(straddle, now="12:00")
check_invariants(slots, "straddling window")
assert selling_minutes(slots) == set(range(1410, 1440)) | set(range(0, 30))

# The tail is only shed under real overflow pressure, never merely for being
# numerically "earlier" than now.
crowd = tail + [w("02:00", "03:00"), w("05:00", "06:00")]
slots = build(crowd, now="12:00")
check_invariants(slots, "tail under pressure")

# ── 6. Adjacent windows merge instead of burning a register ──────────────────
touching = [w("20:00", "21:00", 23), w("21:00", "22:00", 25)]
slots = build(touching)
check_invariants(slots, "touching windows")
assert selling_minutes(slots) == set(range(1200, 1320))
assert len([s for s in slots if s.get("sell")]) == 1, "touching windows should share one slot"
assert slots[[s.get("sell") for s in slots].index(True)]["soc_pct"] == 23, \
    "merged window takes the safer (lower) floor"

# ── 7. write_sell_bit=false omits the bit entirely ───────────────────────────
no_bit = dict(OPTS, write_sell_bit=False)
slots = build(real, opts=no_bit)
check_invariants(slots, "no sell bit")
assert all("sell" not in s for s in slots), "sell bit must be absent when disabled"

# ── 8. Selling windows are capped at the plan's sell power ───────────────────
slots = d.build_desired_slots(one, d._tm("12:00"), OPTS, FLOOR, SOC_HI, 12000)
sell_slot = next(s for s in slots if s.get("sell"))
assert sell_slot["power_w"] == 12000
assert all(s["power_w"] == 30000 for s in slots if not s.get("sell"))

# ── 9. desired_state / charge split ──────────────────────────────────────────
events = [{"time": "05:45", "export": True}, {"time": "06:00", "charge_amps": 1},
          {"time": "08:30", "export": False, "charge_amps": 100}]
assert d.desired_state(events, d._tm("07:00")) == {"export": True, "charge_amps": 1}
assert d.desired_state(events, d._tm("09:00")) == {"export": False, "charge_amps": 100}
assert d.desired_state(events, d._tm("00:10")) == {}
assert d._per_bat(100, 2) == 50 and d._per_bat(1, 2) == 1 and d._per_bat(100, 1) == 100

# The plan's charge_amps knows nothing about the pack's rating, so it is capped.
assert d._per_bat(100, 1, 40) == 40, "must not hand a 40 A pack the plan's 100 A"
assert d._per_bat(100, 2, 40) == 40
assert d._per_bat(30, 1, 40) == 30, "cap must not raise a smaller request"
assert d._per_bat(1, 1, 40) == 1
assert d._per_bat(100, 1, 0) == 100, "cap 0 means no cap"

# Battery count: an absent second battery reports 0 A limits (real Zona-28 read).
zona = {"max_charge_a": 40, "max_discharge_a": 36,
        "bat2_max_charge_a": 0, "bat2_max_discharge_a": 0}
two  = {"max_charge_a": 50, "bat2_max_charge_a": 50}
assert d.resolve_battery_count({"battery_count": None}, zona) == 1
assert d.resolve_battery_count({"battery_count": None}, two) == 2
assert d.resolve_battery_count({"battery_count": None}, None) == 1
assert d.resolve_battery_count({"battery_count": 2}, zona) == 2, "config overrides detection"

# ── 10. idle_floor prefers the map's own non-selling segments ────────────────
m = {"segments": [{"action": "battery_mode", "soc_floor_pct": 18},
                  {"action": "sell_batt",    "soc_floor_pct": 23},
                  {"action": "selling_first", "soc_floor_pct": 18}]}
# The envelope comes from the map, never from local config.
assert d.soc_envelope(m) == (18, 100), "older maps still imply the low end"
assert d.soc_envelope({"soc_min_pct": 15, "soc_max_pct": 90, **m}) == (15, 90), \
    "an explicit envelope wins over the segment scan"
assert d.soc_envelope({}) == (d.SOC_MIN_LAST_RESORT, 100), \
    "a map with nothing to go on falls back cautiously, not to a config guess"

# ── 11. SoC guard fires only inside a window at/below floor+2 ────────────────
assert d._soc_guard_would_fire(real, d._tm("20:30"), 24) is True
assert d._soc_guard_would_fire(real, d._tm("20:30"), 26) is False
assert d._soc_guard_would_fire(real, d._tm("12:00"), 10) is False
assert d._soc_guard_would_fire(real, d._tm("20:30"), None) is False

posted = []
d._post = lambda url, path, body, dry: posted.append((path, body))
slots = build(real, now="20:30")
fired = d.soc_guard(real, slots, d._tm("20:30"), 24, OPTS, "http://x", False)
assert fired is True and len(posted) == 1, posted
path, body = posted[0]
assert path == "/api/settings/tou/4", path       # 20:00 boundary is slot 4
assert body == {"soc_pct": 24, "sell": False}, body

# ── 12. verify_pending round-trips what sync_tou would have written ──────────
fw = {"general": {"limit_control": "Selling First", "solar_sell_enable": True,
                  "max_sell_power_w": 30000},
      "battery": {"max_charge_a": 50, "bat2_max_charge_a": 50},
      "tou": {"enabled": True, "slots": [dict(s, slot=s["slot"]) for s in build(real)]}}
assert d.verify_pending({"tou_slots": build(real), "export_mode": "Selling First",
                         "charge_amps": 100, "tou_enabled": True}, fw, 2) is True
assert d.verify_pending({"export_mode": "Zero Export to CT"}, fw, 2) is False
assert d.verify_pending({"charge_amps": 60}, fw, 2) is False
# solar_sell is verified only when it was part of the write.
assert d.verify_pending({"export_mode": "Selling First", "solar_sell": True},
                        {**fw, "general": {**fw["general"], "solar_sell_enable": False}},
                        2) is False
assert d.verify_pending({"export_mode": "Selling First", "solar_sell": False},
                        {**fw, "general": {**fw["general"], "solar_sell_enable": False}},
                        2) is True

# ── 13. Export is switched by work mode, with the power as a ceiling ─────────
# max_sell_power=0 is not a usable off switch: it does not hold once the mode is
# Selling First, and the plant's limit silently returns. The mode is the switch.
posted.clear()
off_gen = {"limit_control": "Zero Export to CT", "solar_sell_enable": True,
           "max_sell_power_w": 27000}
on_gen  = {"limit_control": "Selling First", "solar_sell_enable": True,
           "max_sell_power_w": 27000}

assert d.exports_now(True, 27.0, True) is True
assert d.exports_now(True, 27.0, False) is False, "outside a window means no export"
assert d.exports_now(False, 27.0, True) is False
assert d.exports_now(True, 0.0, True) is False, "no planned sell power means no export"

assert d.export_power_w(30.0, 27000) == 27000, "plant limit wins"
assert d.export_power_w(20.0, 27000) == 20000, "cap never raises a lower plan"
assert d.export_power_w(30.0, 0) == 30000, "cap 0 means no cap"

# Turning export on switches the mode and sets the ceiling.
assert d.apply_export(True, off_gen, 30.0, 27000, "http://x", False) is True
assert posted[-1][1] == {"limit_control": "Selling First"}, posted[-1]

# Already selling at the right ceiling: nothing to write.
assert d.apply_export(True, on_gen, 27.0, 27000, "http://x", False) is False

# Outside a window the mode goes back to zero-export even though the plan says on.
posted.clear()
assert d.apply_export(True, on_gen, 27.0, 27000, "http://x", False, in_window=False) is True
assert posted[-1][1] == {"limit_control": "Zero Export to CT"}, posted[-1]
assert "max_sell_power_w" not in posted[-1][1], "no ceiling is written when not selling"

# Solar sell tracks the PRICE, which the map already decided, and is therefore
# independent of whether a battery window happens to be open.
posted.clear()
assert d.apply_export(False, on_gen, 27.0, 27000, "http://x", False) is True
assert posted[-1][1] == {"limit_control": "Zero Export to CT",
                         "solar_sell_enable": False}, posted[-1]

posted.clear()
assert d.apply_export(True, dict(off_gen, solar_sell_enable=False), 27.0, 27000,
                      "http://x", False, in_window=False) is True
assert posted[-1][1] == {"solar_sell_enable": True}, posted[-1]
assert "limit_control" not in posted[-1][1], \
    "price says sell, but no window is open — mode must stay put"

# ── 14. Every slot carries a target inside the planner's envelope ────────────
# A slot SOC is a target the inverter drives toward, so a value below the
# battery minimum is a drain instruction, not a safety floor.
lo, hi = 15, 90
slots = build(real, lo=lo, hi=hi)
check_invariants(slots, "clamped envelope")
assert all(lo <= s["soc_pct"] <= hi for s in slots), [s["soc_pct"] for s in slots]

# A window floor beneath the battery minimum is lifted to it.
deep = [w("20:00", "22:00", 5)]
slots = build(deep, lo=lo, hi=hi)
assert next(s for s in slots if s["sell"])["soc_pct"] == lo, "floor must clamp up to batt_min"
assert all(s["soc_pct"] == lo for s in slots), "idle slots sit at the battery minimum"

# ── 15. A slot the plan may export through must not invite a drain ───────────
# This is the failure that emptied a real battery: export power open while the
# active slot targets batt_min, so the pack itself becomes the "surplus".
ev = [{"time": "00:00", "export": False},
      {"time": "09:00", "export": True},      # solar-only selling, no battery window
      {"time": "18:00", "export": False}]
mask = d.export_mask(ev)
assert mask[0] is False and mask[540] is True and mask[1080] is False
assert d._exports_during(mask, 480, 600) is True, "partial overlap counts as exporting"
assert d._exports_during(mask, 0, 480) is False
# A cyclic day: the state at 00:00 comes from the last event, not a default.
assert d.export_mask([{"time": "06:00", "export": False},
                      {"time": "20:00", "export": True}])[0] is True

night = [w("20:30", "22:00", 17)]
slots = build(night, now="12:00", lo=15, hi=90, events=ev)
check_invariants(slots, "export-aware targets")
for s in slots:
    nxt = slots[(slots.index(s) + 1) % len(slots)][ "time"]
    exporting = d._exports_during(mask, d._tm(s["time"]), d._tm(nxt))
    if s.get("sell"):
        assert s["soc_pct"] == 17, "selling window keeps the plan's floor"
    elif exporting:
        assert s["soc_pct"] == 90, \
            f"slot {s['slot']} at {s['time']} may export — must target soft max, got {s['soc_pct']}"
    else:
        assert s["soc_pct"] == 15, "non-exporting slot may carry the house"

# With no events at all nothing can export, so every idle slot may serve load.
slots = build(night, lo=15, hi=90, events=[])
assert all(s["soc_pct"] == 15 for s in slots if not s.get("sell"))

# ── 16. Default policy: no export outside the planned windows ────────────────
# Deye cannot separate "sell surplus solar" from "sell the battery", so the
# safe default declines the former and leaves every idle slot free to carry
# the house at batt_min.
safe = dict(OPTS, sell_solar_outside_windows=False)
slots = build(night, now="12:00", opts=safe, lo=15, hi=90, events=ev)
check_invariants(slots, "battery-windows-only")
assert all(s["soc_pct"] == 15 for s in slots if not s.get("sell")), \
    "idle slots must stay at batt_min so the battery can serve the house"
assert next(s for s in slots if s["sell"])["soc_pct"] == 17

# Export is withheld outside a window and released inside one.
assert d.exports_now(True, 27.0, False) is False
assert d.exports_now(True, 27.0, True) is True

print("all deye dispatcher tests passed")
