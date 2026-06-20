# deye-monitor

A Python CLI tool for reading live data from **Deye hybrid solar inverters** over the local network using the SolarmanV5 protocol.

Outputs data in three formats: human-readable text, JSON, and Prometheus exposition.

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
