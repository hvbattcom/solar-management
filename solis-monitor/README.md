# ☀️ solis-monitor

> **Know exactly what your solar system is doing — in real time.**

`solis-monitor` talks directly to your **Solis hybrid inverter** over the local network
(no cloud, no app, no account required) and gives you a clean snapshot of everything:
PV production, battery state, grid flow, backup loads, temperatures, fault codes, and more.

Output comes in two flavours — human-readable text you can read at a glance,
or Prometheus metrics you can feed straight into Grafana.

---

## Features

- **Human-readable** and **Prometheus** output modes, both template-driven (Jinja2)
- Up to **4 PV strings**, **3-phase** inverter / grid / backup output
- Battery BMS data (SOC, SOH, voltage, current, charge/discharge limits, fault registers)
- Energy counters: today / total for PV, grid import/export, battery charge/discharge, household/backup load
- Full **fault register decoding** (Appendix 5 registers 33116–33120) with human-readable bit labels
- Inverter **status code** and **operating status** bitmask decoding (Appendix 2 & 6)
- **Serial number verification** — output is suppressed if the inverter serial doesn't match config
- **Sanity range filtering** — out-of-range Modbus values are dropped rather than silently corrupted
- Resilient polling: per-block retries, adaptive bisect fallback, all-zeros guard, whole-poll retries

---

## Requirements

- Python 3.9+
- `pymodbus >= 3.0`
- `jinja2 >= 3.1`
- Network access to the Solis LAN logger (Wi-Fi or LAN stick, **not** the cloud API)

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Setup

```bash
git clone https://github.com/<your-username>/solis-monitor.git
cd solis-monitor
pip install -r requirements.txt
cp config.cfg.example config.cfg
```

Edit `config.cfg` and set at minimum `inverter_ip`.

---

## Configuration

Configuration is split across two files:

| File | Tracked | Purpose |
|---|---|---|
| `default_sanity_ranges.cfg` | ✅ yes | Shipped defaults: sanity ranges and zeros-ok flags |
| `config.cfg` | ❌ gitignored | Your site settings: IP, serial, power, overrides |

`default_sanity_ranges.cfg` is loaded first; any key defined in `config.cfg` overrides it.
You never need to touch `default_sanity_ranges.cfg` unless you want to permanently change a default for everyone — per-site tweaks (e.g. `zeros_ok_grid = true`) belong in `config.cfg`.

### config.cfg

```ini
[SolisInverter]
inverter_ip   = 192.168.1.100    # IP of your LAN logger
inverter_port = 502              # 502 for S2-WL-ST; some older loggers use 8899
slave_id      = 1

# Leave blank to skip serial check, or fill in to guard against wrong device
serial = <your_serial>

# Inverter rated power — used to compute load-% values
inverter_power_kw = 10

# default_sanity_ranges.cfg is loaded first; any key set here overrides it
# Example: uncomment to accept all-zero grid readings when grid is physically off
# zeros_ok_grid = true
```

See [config.cfg.example](config.cfg.example) for all available keys.

### default_sanity_ranges.cfg

Contains sanity-range bounds for every sensor type (voltages, currents, power, temperature, frequency) and the `zeros_ok_*` flags for each register block. Readings outside the declared ranges are treated as invalid Modbus garbage and dropped. See [default_sanity_ranges.cfg](default_sanity_ranges.cfg) for the full list.

---

## Usage

```bash
# Human-readable output (default)
python solis-monitor.py

# Prometheus exposition format
python solis-monitor.py --format prometheus

# Skip the Solis-specific template extension
python solis-monitor.py --no-solis-specific
```

### Human output example

```
=== Inverter Status ===
Status: Normal
Serial: 1234ABCD5678
DC Temperature: 42.3°C
Battery Temperature: 28.1°C
Grid Frequency: 50.00Hz

=== PV Status ===
PV1: 342.5V, 8.20A, 2809W
PV2: 338.1V, 8.05A, 2722W
PV3: 0.0V, 0.00A, 0W
PV4: 0.0V, 0.00A, 0W
-------------------------
Total PV (2 active): 5531W

=== Battery Status ===
Battery: 51.2V, -18.40A, -942W, 28.1°C, 72.0%, 100.0% (Charging)

=== Daily Energy Statistics ===
PV Generation:       18.40kWh
Load Consumption:    12.10kWh
Grid Import:         0.30kWh
Grid Export:         5.20kWh
Battery Charge:      4.80kWh
Battery Discharge:   1.10kWh
```

### Prometheus output example

```
# HELP inverter_info Inverter identification
# TYPE inverter_info gauge
inverter_info{brand="solis",serial="1234ABCD5678",status="Normal"} 1

# HELP battery_soc_pct Battery state of charge percent
# TYPE battery_soc_pct gauge
battery_soc_pct{brand="solis",serial="1234ABCD5678"} 72.0

# HELP pv_total_power_w PV total power watts
# TYPE pv_total_power_w gauge
pv_total_power_w{brand="solis",serial="1234ABCD5678"} 5531
...
```

---

## Templates

Output is rendered from Jinja2 templates in `../templates/` (one level above the script, shared across the repository).

| File | Purpose |
|---|---|
| `human` | Human-readable text output |
| `human-solis-specific` | Appended to `human` for Solis-only registers |
| `prometheus` | Prometheus exposition format |
| `prometheus-solis-specific` | Appended to `prometheus` for Solis-only metrics |

The `-solis-specific` variants are appended automatically unless
`--no-solis-specific` is passed. You can edit any template without touching
the Python code. All context variables are documented at the top of each
template file.

---

## Tested Hardware

| Device | Notes |
|---|---|
| Solis S6-EH3P(30-50)K-H | 3-phase hybrid |
| S2-WL-ST LAN logger | Port 502 |

Other Solis models sharing the same Modbus register map should work.
If your model uses zero-based register addressing set `use_zero_based_addressing = true` in config.

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Unhandled exception (details on stderr) |
| `2` | Serial number mismatch |

Warnings and skipped-register diagnostics are written to **stderr**; only the
metrics output goes to **stdout**.

---

## License

MIT — see [LICENSE](LICENSE).
