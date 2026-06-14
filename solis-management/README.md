# solis-management

HTTP API + web UI for writing Solis inverter settings over Modbus TCP.

Reads current state from holding registers and writes back via FC6/FC16.  
Companion to `solis-monitor/` (the read-only poller). Shares the same `config.cfg`.

---

## dispatch.py — Solar plan dispatcher

Runs every 5 minutes via cron on the inverter host. Reads today's dispatch map (written
by the solar planner) and configures Solis S6 TOU discharge slots 5 and 6.

```cron
*/5 * * * * /usr/bin/python3 /path/to/dispatch.py >> /var/log/solar_dispatch.log 2>&1
```

Options:
```
--config PATH   path to config.cfg (default: ../solis-monitor/config.cfg)
--dry-run       print actions without writing to inverter
--time HH:MM    override current time (for testing)
```

**Config** (`[SolisAPI]` section of `config.cfg`):

```ini
port = 5000                                    # solis-api port
mothership_prometheus_api = http://10.100.0.1/ # Prometheus for live SoC (optional)
```

**SoC floor guard**: during sell windows, reads `battery_soc_pct` from Prometheus.
If live SoC ≤ the segment's planned floor, disables the active TOU slot cleanly
before the firmware floor fires (which would cause grid import). Requires
`mothership_prometheus_api` to be set; silently skipped if absent.

**State file** (`dispatch_state.json`):

```json
{
  "date": "2026-06-15",
  "applied_at": "2026-06-15T19:20:01",
  "allow_export": true,
  "soc_disabled": ["21:30"],
  "slots": {"5": {"start":"19:15","end":"22:00",...}, "6": null}
}
```

---

## Quick start

```bash
cd solis-management
python3 solis-api.py
```

Open **http://localhost:5000** in a browser for the UI.

Options:
```
--config PATH   path to config.cfg (default: ../solis-monitor/config.cfg)
--host  ADDR    bind address      (default: 127.0.0.1)
--port  PORT    listen port       (default: 5000)
--debug         Flask debug mode
```

For LAN access: `python3 solis-api.py --host 0.0.0.0`

---

## Web UI

The UI at `/` lets you read and write all settings without curl:

- **Storage Settings** — mode selector, export limit, battery reserve, toggles for allow-export / allow-grid-charge / reserve-switch
- **TOU Charge Slots** — table with enable checkbox, time range, current, cutoff voltage, SOC target; per-row Save
- **TOU Discharge Slots** — same layout

Time fields use a drum scroll-wheel picker (click to open). Supports drag, fling with momentum, and mouse-wheel scroll — 1-minute resolution, always 24h display.

Changes take effect immediately; the UI re-reads settings after each successful save.

---

## API Reference

All write endpoints accept a JSON body with any subset of fields — only fields present are changed, others are left as-is (read-modify-write on bitmask registers).

### `GET /api/settings`

Returns current storage + TOU settings read from holding registers (3 block reads, ~300–600 ms).

```json
{
  "storage": {
    "mode": "Self Use",
    "battery_reserve_on": false,
    "battery_reserve_pct": 20,
    "allow_export": true,
    "allow_grid_charge": false,
    "max_export_power_w": 5000
  },
  "tou": {
    "enabled": false,
    "charge_slots": [
      {"slot": 1, "enabled": false, "start": "00:00", "end": "00:00",
       "current_a": 30.0, "cutoff_v": 48.0, "soc_pct": 20},
      ...
    ],
    "discharge_slots": [ ... ]
  }
}
```

### `GET /api/status`

Full inverter poll — calls `solis-monitor.py --format json` and returns its output (~10 s).  
Use this for decision-making (PV power, battery SOC, grid flow, fault registers, etc.).

### `POST /api/settings/storage`

Update any combination of storage settings in one call.

| Field | Type | Values / range |
|---|---|---|
| `mode` | string | `"Self Use"`, `"Time of Use"`, `"Off Grid"`, `"Feed In Priority"`, `"Peak Shaving"` |
| `battery_reserve_on` | bool | `true` / `false` |
| `battery_reserve_pct` | int | 0 – 100 |
| `allow_export` | bool | `true` / `false` |
| `allow_grid_charge` | bool | `true` / `false` |
| `max_export_power_w` | int | ≥ 0, multiple of 100 |

```bash
# Switch to Feed In Priority and set export limit
curl -X POST http://localhost:5000/api/settings/storage \
  -H "Content-Type: application/json" \
  -d '{"mode": "Feed In Priority", "allow_export": true, "max_export_power_w": 10000}'

# Just toggle grid charge off
curl -X POST http://localhost:5000/api/settings/storage \
  -H "Content-Type: application/json" \
  -d '{"allow_grid_charge": false}'
```

Response: `{"ok": true}` or `{"ok": false, "error": "..."}`

### `POST /api/settings/tou/charge/<N>` and `/discharge/<N>`

Update a single TOU slot. N = 1 – 6.

| Field | Type | Notes |
|---|---|---|
| `enabled` | bool | sets/clears the enable bit in register 43707 |
| `start` | string | `"HH:MM"` |
| `end` | string | `"HH:MM"` |
| `current_a` | float | charge/discharge current in amps |
| `cutoff_v` | float | cutoff voltage in volts |
| `soc_pct` | int | SOC target / floor in percent |

```bash
# Enable charge slot 1: midnight–6am, 30A, 48V cutoff, min SOC 20%
curl -X POST http://localhost:5000/api/settings/tou/charge/1 \
  -H "Content-Type: application/json" \
  -d '{"enabled": true, "start": "00:00", "end": "06:00", "current_a": 30.0, "cutoff_v": 48.0, "soc_pct": 20}'

# Disable without changing any other slot parameters
curl -X POST http://localhost:5000/api/settings/tou/charge/1 \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

---

## Register map

| Setting | Register | Scale |
|---|---|---|
| Battery Reserve % | 43024 | raw = % |
| Max Export Power | 43074 | raw × 100 = W (100 W granularity) |
| Mode / flags bitmask | 43110 | bit 0=Self Use, 1=TOU, 2=Off Grid, 4=Reserve On, 5=Grid Charge, 6=Feed In, 11=Peak Shaving |
| Allow Export | 43483 | bit 3 |
| TOU enable bitmask | 43707 | bits 0-5 = charge slots 1-6, bits 6-11 = discharge slots 1-6 |
| TOU charge slot N | 43708 + (N-1)×7 | 7 regs: SOC%, I×10, V×10, start_h, start_m, end_h, end_m |
| TOU discharge slot N | 43750 + (N-1)×7 | same layout |

---

## Running as a systemd service

A ready-to-use unit file is included at `solis-management/solis-management.service`.

**Install:**

```bash
sudo cp solis-management/solis-management.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now solis-management
```

**Check status / logs:**

```bash
systemctl status solis-management
journalctl -u solis-management -f
```

**Stop / restart:**

```bash
sudo systemctl stop    solis-management
sudo systemctl restart solis-management
```

The service runs as user `ilia`, binds to `0.0.0.0:5000`, and restarts automatically on failure (10 s delay).  
To change the bind address or port, edit `ExecStart` in the unit file and run `sudo systemctl daemon-reload`.

---

## Requirements

```
pymodbus >= 3.0
flask    >= 3.0
```

Install from the repo root: `pip install -r requirements.txt`
