# solar-management

A collection of tools for monitoring and managing hybrid solar inverters over Modbus TCP.

Currently targets **Solis** inverters. Designed to be extended to other brands (Deye, etc.).

---

## Repository layout

```
solar-management/
├── solis-monitor/        # Read-only inverter poller — metrics, status, fault registers
├── solis-management/     # Read-write API + web UI — storage mode, TOU slots, export limits
├── templates/            # Jinja2 output templates (human, JSON, Prometheus)
└── requirements.txt      # Shared Python dependencies
```

---

## Components

### `solis-monitor/`

Polls inverter holding registers and outputs formatted data.  
Supports human-readable text, JSON, and Prometheus exposition formats via Jinja2 templates.  
Intended to feed a time-series database (e.g. InfluxDB, Prometheus) and dashboards (Grafana).

See [solis-monitor/README.md](solis-monitor/README.md) for full details.

### `solis-management/`

Flask HTTP API + single-page web UI for writing inverter settings.  
Reads current state and writes back over Modbus TCP (FC6/FC16).  
Designed to be embedded in Grafana as an HTML panel.

See [solis-management/README.md](solis-management/README.md) for full details.

### `templates/`

Jinja2 templates used by `solis-monitor` to format output:

| Template | Description |
|---|---|
| `human` | Human-readable text report |
| `human-solis-specific` | Extended text with Solis-specific fields |
| `json` | JSON output for API consumers |
| `prometheus` | Prometheus text exposition format |
| `prometheus-solis-specific` | Extended Prometheus metrics |

---

## Requirements

```
jinja2    >= 3.1
pymodbus  >= 3.0
flask     >= 3.0
```

Install:

```bash
pip install -r requirements.txt
```

---

## Configuration

Both components share a single config file at `solis-monitor/config.cfg`.  
Copy `solis-monitor/config.cfg.example` and edit the inverter IP, port, and slave ID.
