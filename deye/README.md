# deye

Tools for **Deye hybrid solar inverters**, talking SolarmanV5 to the datalogger over the
local network:

| File | Role |
|---|---|
| `deye-monitor.py` | Read-only poller / CLI (human, JSON, Prometheus output) |
| `deye-api.py` | Read-write Flask HTTP API + management UI |
| `dispatcher.py` | 5-minute cron that applies the solar planner's dispatch map |

The monitor is documented below; the dispatcher has its own section at the end.

## Requirements

- Python 3.8+
- A Deye (or rebranded) hybrid inverter with a SOLARMAN Wi-Fi datalogger on your local network

Install the one external dependency:

```bash
pip install -r requirements.txt
```

## Setup

Copy the example config and fill in your inverter details:

```bash
cp config.cfg.example config.cfg
```

Edit `config.cfg`:

```ini
[DeyeInverter]
inverter_ip = 192.168.1.x       # IP of your SOLARMAN datalogger
inverter_sn = 1234567890        # Serial number from the datalogger label
verbose = false                 # Set to true for debug register-read output
```

The serial number is printed on the sticker on the SOLARMAN Wi-Fi stick/logger.

## Usage

```bash
python deye-monitor.py [--config config.cfg] [--format human|json|prometheus]
```

### Options

| Option | Default | Description |
|---|---|---|
| `--config` | `config.cfg` | Path to the INI config file |
| `--format` | `human` | Output format: `human`, `json`, or `prometheus` |

### Examples

```bash
# Human-readable dashboard
python deye-monitor.py

# JSON output (e.g. pipe into jq)
python deye-monitor.py --format json | jq .battery

# Prometheus exposition format
python deye-monitor.py --format prometheus
```

## Output

### Human (`--format human`)

```
=== Deye Inverter Status ===
Status: GridConnected
DC Temperature: 38.2°C  AC Temperature: 41.5°C  Battery Temperature: 28.0°C

=== PV Input Values ===
PV1: 380.1V, 4.50A, 1710W
PV2: 375.8V, 3.20A, 1210W
...
Total PV (2 active): 2920W

=== Battery Status ===
Battery1: 52.4V, 18.50A, 970W, 28.0°C, 75.0%, 100.0%, 1  (Discharging)

=== Time of Use Programming ===
Grid Charge Enable: No   Solar Sell Enable: Yes
| Grid | Gen | Sell |    Time     |   Pwr    | SOC % |
|  ✓   |     |   ✓  | 00:00|06:00 |     3000 |  20%  |
...
```

### JSON (`--format json`)

```json
{
  "serial": "1234567890",
  "system": { "status": "GridConnected", "dc_temperature_c": 38.2, ... },
  "pv": { "active_strings": 2, "total_power_w": 2920.0, "strings": [...] },
  "battery": { "battery1": { "soc_percent": 75.0, "power_w": 970.0, ... } },
  "daily_energy_kwh": { "pv": 12.4, "load": 8.1, ... },
  "tou": { "slots": [...] }
}
```

### Prometheus (`--format prometheus`)

Emits standard Prometheus text exposition with labels `serial` and `phase`/`string`/`id` where applicable. Suitable for scraping with a `textfile_collector` or a cron-based push to a Pushgateway.

```
deye_pv_power_w{string="PV1",serial="1234567890"} 1710.000
deye_battery_soc_percent{id="1",serial="1234567890"} 75.000
deye_grid_total_power_w{serial="1234567890"} -450.000
...
```

## Data collected

| Group | Registers |
|---|---|
| PV strings | Voltage, current, power for PV1–PV4; daily & lifetime generation |
| Inverter | Per-phase V/A/W (L1–L3); DC/AC temperatures |
| Grid | Per-phase V/A/W via external CT; total power; daily & lifetime import/export |
| Load | Per-phase V/A/W (L1–L3); daily & lifetime consumption |
| Battery | Voltage, current, power, temperature, SOC (battery 1 & 2); daily & lifetime charge/discharge |
| Time of Use | 6 ToU slots (time window, charge power, SOC target, grid/gen/sell flags) |

## Templates

Output is rendered from plain-text templates in the `templates/` directory:

| File | Used for |
|---|---|
| `templates/human.txt` | `--format human` |
| `templates/json.txt` | `--format json` |
| `templates/prometheus.txt` | `--format prometheus` |

Templates use Python's `str.format_map` syntax (`{key}`, `{nested[key]:.2f}`). You can edit them freely to add, remove, or reformat fields without touching the Python code.

---

## Dispatcher (`dispatcher.py`)

Applies the solar planner's dispatch map to the inverter. The planner `POST`s a map to
`deye-api.py`'s `/api/map`; a cron job then runs the dispatcher every 5 minutes, which
derives the desired inverter state and writes only what has changed:

```
*/5 * * * *  /usr/bin/python3 /path/to/solar-management/deye/dispatcher.py
```

```bash
python3 dispatcher.py --show                 # print the map + resulting schedule, change nothing
python3 dispatcher.py --dry-run              # log the writes it would make
python3 dispatcher.py --time 20:30           # evaluate as if it were 20:30
python3 dispatcher.py --instance GS48        # pick a specific plant's map
```

### How the plan maps onto Deye's Time of Use

The map is brand-neutral: battery **discharge windows** plus a timeline of
**events** (export on/off, charge current). Solis can hold that as a schedule; Deye
cannot, so this dispatcher does not try.

Deye has 6 TOU time **points** tiling the day, with no per-slot enable. Encoding
windows as boundaries costs two registers each, caps the day at roughly three
windows, and makes every window edge a rewrite. Instead:

| | |
|---|---|
| slot **times** | fixed scaffolding — written once, never touched |
| slot **values** | the live control signal, rewritten when intent changes |

All six slots always carry **identical** values, so crossing a boundary between
runs is a no-op and the dispatcher's 5-minute cadence is the only granularity
that matters. Windows cost nothing: twelve are the same as two, and one landing
next to a boundary — previously a real hazard — no longer means anything.

### The two controls

A slot's SOC is a **target the inverter drives toward**, not a floor it refuses
to cross. Combined with the per-slot sell bit that gives two orthogonal controls:

| Control | Question |
|---|---|
| target vs SoC | may the battery be **dispatched** at all? |
| sell bit | may what it dispatches reach the **grid**? |

Which covers every state the plan asks for:

| Intent | target | sell bit |
|---|---|---|
| Carrying the house | below SoC (`batt_min`) | off |
| Banking solar | above SoC (`batt_soft_max`) | off |
| Selling the battery | the window's floor | on |

Register 142 (work mode) is pinned to **Selling First** and never written —
selling is gated by the sell bit. Register 145 (solar sell) follows the plan's
price signal directly, and 143 carries the sell ceiling.

Every slot is written with grid charge **off**: a set grid-charge bit flips that
slot's SOC from "discharge down to" into "charge up to".

### Hold or release, outside a window

Whether the pack is held for banking or released to the house is decided from
the **live power balance**, not the clock — a cloudy afternoon needs the battery
on the house exactly as much as midnight does:

```
surplus = -(battery + grid)      what is charging the pack plus what is leaving
```

Held above +300 W of surplus, released once the house draws from the grid, and
between the two the previous answer stands so it cannot flap. All of it read
from the inverter itself; there is no Prometheus dependency.

### What it writes

| Plan input | Deye setting | Register |
|---|---|---|
| in/out of a discharge window | slot SOC target ×6, sell bit ×6 | 166–171, 172–177 |
| (scaffolding, written once) | slot times ×6 | 148–153 |
| (pinned, never toggled) | work mode = Selling First | 142 |
| `export: true/false` | solar sell | 145 |
| `sell_kw` | sell ceiling | 143 |
| `charge_amps` | max battery charge current, split across batteries | 108, 243 |
| (must be on) | TOU enable + all weekdays | 146 |

Writes go through `deye-api.py` with an `X-Dispatcher: 1` header, which gets them past the
auto-management guard that blocks manual writes while the plan is in charge.

### Safety

- **SoC guard** — inside a window, if the pack is within 2 % of the plan's floor the sell bit
  is withheld. Deye stops at the target on its own, so this is a backstop.
- **Only what changed is written** — the API diffs the 30-register TOU block and writes just
  the differing registers, so a settled day costs no EEPROM cycles.
- **Auto-management** — while enabled (`/api/auto-managed`), manual writes through the UI
  and API are refused so they cannot fight the dispatcher.

### Configuration

The dispatcher reads the `[DeyeAPI]` section of `config.cfg`: `port`,
`battery_count`, `max_charge_amps` and `max_sell_power_w`. See `config.cfg.example`.

The battery's SoC envelope is *not* among them: `soc_min_pct` / `soc_max_pct` come from the
map, since they are the same `batt_min` / `batt_soft_max_soc` the plan was built against and
a local copy could only drift out of agreement with it.

Two of those are worth setting deliberately per plant:

- **`max_charge_amps`** — the plan's `charge_amps` is a planner-side figure (it comes from
  `tou_discharge_amps`) that knows nothing about the pack's rating, so a plant whose battery
  accepts 40 A can be handed 100 A. Set this to what the battery actually takes.
- **`battery_count`** — `auto` asks the inverter, treating 0 A limits on the second battery
  as "not installed". Getting this wrong is not cosmetic: assuming two batteries on a
  single-battery plant halves the charge current all day.

