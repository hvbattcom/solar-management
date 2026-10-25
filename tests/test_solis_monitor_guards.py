"""Offline tests for the bit-flip guards in solis/solis-monitor.py — no inverter.

Feeds read_values() simulated register maps carrying the corrupted reads seen
in production (single flipped high bits) and checks they never reach the
output, while real readings and legitimate counter resets pass through.

Run directly:  python3 tests/test_solis_monitor_guards.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("solis_monitor", _ROOT / "solis" / "solis-monitor.py")
m     = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

m.POLL_RETRY_DELAY = 0

with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as f:
    f.write("[SolisInverter]\ninverter_ip = 127.0.0.1\ninverter_power_kw = 40\nmppt_count = 2\n")
cfg = m.load_config(Path(f.name))

# ── Simulated inverter ────────────────────────────────────────────────────────

SENSOR = {s["key"]: s for s in m.SENSORS}

def regmap(**vals):
    """A full register map, every sensor 0 except the given decoded values."""
    rm = {r: 0 for s in m.SENSORS for r in s["registers"]}
    for key, v in vals.items():
        s = SENSOR[key]
        raw = round(v / s.get("scale", 1))
        if len(s["registers"]) == 2:
            raw &= 0xFFFFFFFF
            rm[s["registers"][0]], rm[s["registers"][1]] = raw >> 16, raw & 0xFFFF
        else:
            rm[s["registers"][0]] = raw & 0xFFFF
    return rm

reads = []

def read_registers(client, cfg):
    return reads.pop(0)

m.read_registers = read_registers

def poll(*maps):
    """Run one read_values() over the given successive reads; all must be used."""
    reads[:] = list(maps)
    values, _ = m.read_values(None, cfg)
    assert not reads, f"{len(reads)} read(s) left unused"
    return values

DAY = dict(pv_voltage_1=600, pv_current_1=10, pv_voltage_2=600, pv_current_2=10)

# ── Range: inverter-side powers capped at 1.5 × rated ────────────────────────

bad = regmap(battery_power=1073741824, inverter_active_power=268438926)
v = poll(bad, bad, bad)
assert v["battery_power"] is None and v["inverter_active_power"] is None, v
v = poll(regmap(battery_power=-20600, inverter_active_power=41060))
assert v["battery_power"] == -20600 and v["inverter_active_power"] == 41060, "real peaks pass"
v = poll(regmap(battery_power=60001), regmap(battery_power=-3600))
assert v["battery_power"] == -3600, "just over 1.5 × 40 kW → re-read"
v = poll(regmap(household_load_power=65000, grid_power_total=90000))
assert v["household_load_power"] == 65000 and v["grid_power_total"] == 90000, \
    "grid / household load are not limited by the inverter rating"

# ── PV total must match the strings ───────────────────────────────────────────

v = poll(regmap(pv_total_power=32768), regmap(pv_total_power=0))
assert v["pv_total_power"] == 0, "night flip is re-read and the clean read wins"
bad = regmap(pv_total_power=4096)
v = poll(bad, bad, bad)
assert v["pv_total_power"] is None, "persistent mismatch is dropped"
v = poll(regmap(pv_total_power=12900, **DAY))
assert v["pv_total_power"] == 12900, "normal daytime disagreement passes"
bad = regmap(pv_total_power=12000 + 32768, **DAY)
v = poll(bad, bad, bad)
assert v["pv_total_power"] is None, "daytime flip into a believable range"

# ── Energy counters ───────────────────────────────────────────────────────────

clock = {"t": 1000.0}
m.time.monotonic = lambda: clock["t"]
m._counters.clear()     # forget the zeros remembered by the polls above

def tick(seconds=60):
    clock["t"] += seconds

v = poll(regmap(load_energy_total=50000, household_load_energy_month=500))
assert v["load_energy_total"] == 50000

tick()
bad = regmap(load_energy_total=50000 + 2**28, household_load_energy_month=500)
v = poll(bad, bad, bad)
assert v["load_energy_total"] == 50000, "upward flip held at last good value"

tick()
v = poll(regmap(load_energy_total=50001, household_load_energy_month=501))
assert v["load_energy_total"] == 50001, "normal step accepted"

tick()
bad = regmap(load_energy_total=50001 - 128, household_load_energy_month=501)
v = poll(bad, bad, bad)
assert v["load_energy_total"] == 50001, "lifetime total never goes down"

tick()
v = poll(regmap(load_energy_total=50001, household_load_energy_month=0))
assert v["household_load_energy_month"] == 0, "month counter reset is accepted"

tick()
bad = regmap(load_energy_total=50001, household_load_energy_month=1048576)
v = poll(bad, bad, bad)
assert v["household_load_energy_month"] == 0, "the 1048576 kWh flip seen at Tishoplast"

tick(3600)
v = poll(regmap(load_energy_total=50040, household_load_energy_month=40))
assert v["load_energy_total"] == 50040, "an hour at 40 kW is plausible"

# Unreadable counter: hold the last value rather than render 0
tick()
rm = regmap(load_energy_total=50041)
del rm[SENSOR["household_load_energy_month"]["registers"][0]]
del rm[SENSOR["household_load_energy_month"]["registers"][1]]
v = poll(rm)
assert v["household_load_energy_month"] == 40, v["household_load_energy_month"]

# A genuinely new value (e.g. counters reset on the inverter) is accepted
# once it has been seen on enough consecutive polls
for i in range(m.COUNTER_REBASE_POLLS):
    tick()
    bad = regmap(load_energy_total=7)
    assert poll(bad, bad, bad)["load_energy_total"] == 50041, f"held on poll {i + 1}"
tick()
assert poll(regmap(load_energy_total=7))["load_energy_total"] == 7, "rebased"
tick()
assert poll(regmap(load_energy_total=8))["load_energy_total"] == 8

print("all solis monitor guard tests passed")
