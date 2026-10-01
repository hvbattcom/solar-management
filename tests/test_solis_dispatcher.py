"""Offline tests for solis/dispatcher.py — no inverter, no network.

Drives main() run after run against a simulated inverter to check the SoC
guard: a window it shuts stays shut, instead of the slot sync re-enabling it
on the next run and the slot flipping every five minutes.

Run directly:  python3 tests/test_solis_dispatcher.py
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("solis_dispatcher", _ROOT / "solis" / "dispatcher.py")
d     = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(d)

MAP = {"date": "2026-10-02", "instance_id": "GS48", "sell_kw": 0, "events": [],
       "tou_slots": [{"start": "07:30", "end": "08:30", "soc_floor_pct": 17, "amps": 86},
                     {"start": "17:45", "end": "18:00", "soc_floor_pct": 17, "amps": 86}]}

# ── Simulated inverter ────────────────────────────────────────────────────────

fw    = {"storage": {}, "battery": None,
         "tou": {"discharge_slots": [{"slot": n, "enabled": False, "start": "00:00",
                                      "end": "00:00", "soc_pct": 17, "current_a": 86.0}
                                     for n in range(1, 7)]}}
posts = []

def _post(api_url, path, body, dry_run):
    posts.append(path)
    assert path == "/api/settings/tou/discharge/all", path
    slots = {s["slot"]: s for s in fw["tou"]["discharge_slots"]}
    for u in body:
        slots[u["slot"]].update(u)

soc = {"now": 60}
d._post        = _post
d.read_fw      = lambda url: json.loads(json.dumps(fw))
d.get_soc      = lambda url, inst: soc["now"]
d.auto_managed = lambda url: True
d.load_config  = lambda p: {"api_url": "http://stub", "prom_url": "http://stub"}
d._STATE_FILE  = Path(tempfile.mkdtemp()) / "dispatcher_state.json"

current_map = {"m": MAP}
d.load_map  = lambda inst: current_map["m"]

def run(at: str, level: int) -> list:
    """One cron run at HH:MM with the pack at `level` %; returns the POSTs made."""
    soc["now"] = level
    posts.clear()
    sys.argv = ["dispatcher.py", "--time", at]
    d.main()
    return list(posts)

def enabled(start: str) -> bool:
    return any(s["enabled"] and s["start"] == start for s in fw["tou"]["discharge_slots"])

# ── Plan is applied ───────────────────────────────────────────────────────────

assert run("07:00", 60), "first run writes the plan"
assert enabled("07:30") and enabled("17:45")
assert run("07:05", 60) == [], "nothing changed — no write"

# ── Guard shuts the active window ─────────────────────────────────────────────

assert run("07:40", 19), "SoC 19 ≤ floor 17 + 2 — guard must write"
assert not enabled("07:30"), "the active window is shut"
assert enabled("17:45"), "a later window is untouched"

# ── …and it stays shut while SoC recovers inside the window ───────────────────

for t, lvl in (("07:45", 21), ("07:50", 25), ("08:00", 19), ("08:20", 30)):
    assert run(t, lvl) == [], f"{t}: no write — the latch holds (was flipping before)"
    assert not enabled("07:30"), f"{t}: window still shut"

# ── After it ends, and for later windows, nothing is re-enabled or disturbed ──

assert run("09:00", 40) == [], "an ended latched window is not rewritten"
assert not enabled("07:30") and enabled("17:45")

# ── The evening window is guarded on its own ──────────────────────────────────

assert run("17:50", 18), "evening window near floor — guard writes"
assert not enabled("17:45")
assert run("17:55", 25) == []

state = json.loads(d._STATE_FILE.read_text())
assert state["soc_latched"] == ["07:30-08:30", "17:45-18:00"], state["soc_latched"]

# ── A new day's map starts with no latch ──────────────────────────────────────

current_map["m"] = {**MAP, "date": "2026-10-03"}
assert run("07:00", 60), "next day: windows written again"
assert enabled("07:30") and enabled("17:45"), "latch did not leak into the next day"

# ── Without a SoC reading the guard stays out of it ───────────────────────────

d.get_soc = lambda url, inst: None
assert run("07:40", 0) == [], "no SoC → no guard, plan stands"
assert enabled("07:30")

print("all solis dispatcher tests passed")
