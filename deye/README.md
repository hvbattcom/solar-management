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

The map is brand-neutral: a list of battery **discharge windows** plus a timeline of
**events** (export on/off, battery charge current). Solis and Deye reach that state very
differently.

|  | Solis | Deye |
|---|---|---|
| Slot model | 6 discharge windows, each with its own start **and** end | 6 time **points** — slot N runs until slot N+1's time, slot 6 wraps past midnight to slot 1 |
| Coverage | time outside an enabled window is plain self-use | the six slots always tile the full 24 h |
| Per-slot enable | bit in the TOU switch register (43707) | none — only a global schedule switch (register 146) |
| Discharge limit | per-slot current (A) | per-slot power (W) |
| Slot SOC field | discharge floor — a limit | **a target the inverter drives toward** |
| Export control | `allow_export` bit — a permission | work mode — a command to export, sourcing from the battery |

Three consequences shape the implementation, and the last two were learned the hard way:

**A window costs two registers, not one.** Turning selling on and back off both need a
boundary, so six registers hold roughly **three** windows where Solis holds six. Overflow is
handled the same way as Solis: windows already finished are dropped first, then the latest
windows are deferred and slide in on a later run as earlier ones end. Unused boundaries are
padded by splitting the widest segment at its midpoint, which inherits that segment's
settings and so changes no behaviour.

**A slot's SOC is a target, not a floor.** Set it below where the battery currently sits and
the inverter works to get there — with the work mode selling, that means exporting the pack
to the grid. So every slot carries a deliberate value: the plan's floor inside a selling
window, and the battery minimum elsewhere, where the pack is free to carry the house.

**Export is switched with the work mode, and only inside a planned window.** Deye has no
equivalent of Solis's `allow_export` permission: "Selling First" is an instruction to export
up to `max_sell_power_w`, taking from PV *and the battery*. It is safe only while every slot
it spans targets at or above the battery's current level. And because Deye cannot say "sell
the solar but not the battery" — one mode governs both — the default is to not export outside
the plan's own discharge windows. See `sell_solar_outside_windows` for that trade-off.

> `max_sell_power_w` cannot serve as the on/off control. The register accepts 0 and holds it
> under Zero Export to CT, but once the mode is Selling First the plant's configured limit
> returns on its own within minutes and export resumes at full power. It is a ceiling only.

**Every slot is written with grid charge off.** The planner has no grid-charging concept, and
a set grid-charge bit flips that slot's SOC from "discharge down to" into "charge up to".

### What it writes

| Plan input | Deye setting | Register |
|---|---|---|
| discharge windows | TOU time / power / SOC / control | 148–153, 154–159, 166–171, 172–177 |
| (schedule must be live) | TOU enable + all weekdays | 146 |
| `export: true/false`, inside a window | work mode; sell ceiling; solar sell held on | 142, 143, 145 |
| `charge_amps` | max battery charge current, split across batteries | 108, 243 |

Writes go through `deye-api.py` with an `X-Dispatcher: 1` header, which is what gets them
past the auto-management guard that blocks manual writes while the plan is in charge. Only
registers whose value actually differs are written, so a steady day costs no EEPROM cycles.

### Safety

- **SoC guard** — if the battery reaches its floor + 2 % inside a live selling window, that
  slot's floor is raised to the measured SoC and its sell bit cleared, halting the drain
  without disturbing the rest of the schedule. Deye enforces the floor itself, so this is a
  backstop rather than the primary mechanism. It needs `mothership_prometheus_api` set;
  without it the plan is applied blind.
- **Write verification** — every write is re-read on the next run and retried if the
  firmware did not take it.
- **Auto-management** — while enabled (`/api/auto-managed`), manual writes through the UI
  and API are refused so they cannot fight the dispatcher.

### Configuration

The dispatcher reads the `[DeyeAPI]` section of `config.cfg`: `port`,
`mothership_prometheus_api`, `battery_count`, `max_charge_amps`, `max_sell_power_w`,
`idle_floor_pct`, `soc_max_pct`, `sell_solar_outside_windows` and `write_sell_bit`. See
`config.cfg.example` for what each one does.

Three of those are worth setting deliberately per plant:

- **`max_charge_amps`** — the plan's `charge_amps` is a planner-side figure (it comes from
  `tou_discharge_amps`) that knows nothing about the pack's rating, so a plant whose battery
  accepts 40 A can be handed 100 A. Set this to what the battery actually takes.
- **`battery_count`** — `auto` asks the inverter, treating 0 A limits on the second battery
  as "not installed". Getting this wrong is not cosmetic: assuming two batteries on a
  single-battery plant halves the charge current all day.
- **`sell_solar_outside_windows`** — off by default. Turning it on sells surplus PV outside
  the plan's discharge windows, but the only way to protect the battery there is to pin those
  slots to `soc_max_pct`, which stops the pack serving the house. On a plant whose prices sit
  above `min_sell_price` most of the day that means importing overnight, so weigh it against
  how much surplus actually survives charging the battery.

Set `write_sell_bit = false` if your firmware has no per-slot Sell column — selling is then
governed solely by the work-mode register, which the dispatcher drives from the plan's export
events either way.
