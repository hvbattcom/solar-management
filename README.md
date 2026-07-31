# solar-management

A collection of tools for monitoring and managing hybrid solar inverters over Modbus TCP / SolarmanV5.

Supports **Solis** inverters (Modbus TCP) and **Deye** inverters (SolarmanV5 dataloggers), plus
detection of a [Battery Emulator](https://github.com/dalathegreat/Battery-Emulator) on the LAN.

---

## Repository layout

```
solar-management/
├── discover.sh            # LAN scan for Solis/Deye inverters + Battery Emulator; writes found.yaml
├── deploy.sh               # One-shot install + systemd service setup (uses found.yaml)
├── solis/                  # Solis monitor, API, dispatcher, config
├── deye/                   # Deye monitor, API, config
├── templates/               # Jinja2/format output templates (human, JSON, Prometheus)
└── requirements.txt         # Shared Python dependencies
```

---

## Components

### `solis/`

Read-only poller (`solis-monitor.py`), read-write Flask API (`solis-api.py`), and
**`dispatcher.py`** — a 5-minute cron dispatcher that reads the solar planner's dispatch map,
derives the desired inverter state from time-ordered events, and applies only what has changed
(TOU discharge slots, export flag, battery charge current).

### `deye/`

Read-only poller (`deye-monitor.py`) and read-write Flask API (`deye-api.py`) for Deye
hybrid inverters, talking SolarmanV5 to the datalogger. See [deye/README.md](deye/README.md).

### `templates/`

Jinja2 templates used to format monitor output:

| Template | Description |
|---|---|
| `human`, `human-solis-specific`, `human-deye-specific` | Human-readable text reports |
| `json` | JSON output for API consumers |
| `prometheus`, `prometheus-solis-specific`, `prometheus-deye-specific` | Prometheus text exposition |

---

## Requirements

```
jinja2         >= 3.1
pymodbus       >= 3.0
pysolarmanv5
flask          >= 3.0
```

Install:

```bash
pip install -r requirements.txt
```

System dependencies: `nmap`, `curl`, `python3`.

---

## Device discovery (`discover.sh`)

Scans the LAN for a Solis inverter (port 502), a Deye datalogger (port 8899), and a Battery
Emulator (port 80), and prints what it finds:

```bash
./discover.sh                              # auto-detect local subnet
./discover.sh 192.168.22.0/24              # explicit subnet
./discover.sh 192.168.22.0/24 5            # explicit subnet + probe timeout (seconds)
./discover.sh --generate-config            # scan + write solis/config.cfg and deye/config.cfg
```

**Discovery can be flaky** — a single scan sometimes misses the inverter, sometimes the
battery, sometimes both, depending on network timing. To make that survivable, every run
(whether or not `--generate-config` is passed) merges its results into **`found.yaml`** in
the project root: a device recorded on an earlier run is kept even if a later scan doesn't
see it, so re-running `./discover.sh` a few times converges on having everything recorded
without losing what was already found.

`found.yaml` looks like:

```yaml
solis_found: true
solis_ip: "192.168.22.50"
solis_serial: "1111111111"
solis_last_seen: "2026-07-31T13:13:05Z"

deye_found: false
deye_ip: ""
deye_sn: ""
deye_last_seen: ""

battery_found: true
battery_ip: "192.168.22.60"
battery_mac: "AA:BB:CC:DD:EE:01"
battery_last_seen: "2026-07-30T09:00:00Z"
```

If your scan is unreliable, run it a few times until `found.yaml` shows everything you need:

```bash
./discover.sh
./discover.sh   # run again if something was missing
./discover.sh   # ...as many times as needed
```

Once `found.yaml` has what you need, use `--from-file` to (re)generate `config.cfg` files
without re-scanning:

```bash
./discover.sh --generate-config --from-file
```

`found.yaml` is local, git-ignored state (like `config.cfg`) — it is not committed.

---

## Deployment (`deploy.sh`)

```bash
sudo ./deploy.sh
```

Installs system + Python dependencies, loads devices from `found.yaml` (scanning once first
if it doesn't exist yet), generates `config.cfg` for whichever inverter(s) were found, sets
up and starts the `solar-management` systemd service, and health-checks it.

If `found.yaml` already exists, `deploy.sh` reuses it instead of scanning again — so if
discovery was flaky, run `./discover.sh` a few times beforehand until it has everything,
then run `deploy.sh`. Delete `found.yaml` (or re-run `./discover.sh`) to force a fresh scan,
e.g. after a device's IP changes.

---

## Configuration

Each inverter has its own config file, generated from an `.example` template:

- `solis/config.cfg` (from `solis/config.cfg.example`)
- `deye/config.cfg` (from `deye/config.cfg.example`)

These are normally written for you by `discover.sh --generate-config` / `deploy.sh`, but can
also be copied and edited by hand.
