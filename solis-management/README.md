# solis-management

HTTP API + web UI for writing Solis inverter settings over Modbus TCP.

Reads current state from holding registers and writes back via FC6/FC16.  
Companion to `solis-monitor/` (the read-only poller). Shares the same `config.cfg`.

---

## dispatch.py — Solar plan dispatcher

Runs every 5 minutes via cron on the inverter host. Reads today's dispatch map (written
by the solar planner) and configures all 6 Solis S6 TOU discharge slots.

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
management_url = http://localhost:5000          # solis-api base URL
```

**Auto-managed gate** — First thing each run: queries `GET /api/auto-managed`. If the toggle
is OFF, dispatcher exits immediately without reading firmware or making any changes. This lets
you take manual control of the inverter from the web UI.

**Sliding-window slot recycling** — The firmware has 6 TOU discharge slots; a daily plan can
have more than 6 sell segments. The dispatcher fills slots 1-6 with the first 6 upcoming
segments. When a slot's window ends it is immediately recycled to the next deferred segment —
one write per transition, no cascade across other slots. Plans with ≤ 6 sell windows are
unaffected.

**Sticky no-write** — A SHA-1 of the map is stored in `dispatch_state.json`. If the plan
hasn't changed and no slot has expired, the run exits with "all slots up to date — skipping"
and makes zero firmware writes.

**`X-Dispatcher: 1` header** — All POST requests from `dispatch.py` carry this header so the
solis-api can distinguish dispatcher calls from manual/UI calls when auto-management is ON.

**SoC floor guard** — During `sell_batt` windows, reads `battery_soc_pct` from Prometheus.
If live SoC ≤ the segment's `soc_floor_pct`, disables the active TOU slot cleanly before
the firmware floor fires (which would cause grid import). Requires `mothership_prometheus_api`;
silently skipped if absent.

**Retry on failure** — HTTP calls retry once; on second failure exits without saving state so
the next cron run retries automatically.

**State file** (`dispatch_state.json`):

```json
{
  "date": "2026-06-17",
  "plan_hash": "25d46c5550ad",
  "slot_assignment": {
    "1": {"start": "07:30", "end": "08:00", "action": "sell_solar", ...},
    "2": {"start": "08:00", "end": "08:15", "action": "sell_batt",  ...},
    "3": {"start": "08:15", "end": "10:45", "action": "sell_solar", ...},
    "4": {"start": "16:30", "end": "19:15", "action": "sell_solar", ...},
    "5": {"start": "19:15", "end": "22:00", "action": "sell_batt",  ...},
    "6": {"start": "22:15", "end": "23:15", "action": "sell_batt",  ...}
  },
  "soc_disabled": ["21:30"]
}
```

When a plan has > 6 sell segments, some slots will initially be `null` (deferred) and will
be filled in as earlier slots expire during the day.

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

- **Automatically managed toggle** (iOS-style switch, top of page) — when ON, the dispatcher
  controls the inverter; the rest of the page is greyed out and a banner is shown. Manual edits
  are blocked at the API level (the dispatcher bypasses this via `X-Dispatcher: 1`). The toggle
  auto-refreshes the page every 30 seconds while active. State is persisted in `auto_managed.json`.
- **Storage Settings** — mode selector, export limit, battery reserve, toggles for allow-export / allow-grid-charge / reserve-switch
- **TOU Charge Slots** — table with enable checkbox, time range, current, cutoff voltage, SOC target; per-row Save
- **TOU Discharge Slots** — same layout

Time fields use a drum scroll-wheel picker (click to open). Supports drag, fling with momentum, and mouse-wheel scroll — 1-minute resolution, always 24h display.

Changes take effect immediately; the UI re-reads settings after each successful save.

---

## API Reference

All write endpoints accept a JSON body with any subset of fields — only fields present are changed, others are left as-is (read-modify-write on bitmask registers).

> **Auto-managed guard**: when auto-management is ON, write endpoints return HTTP 403 unless the
> request carries `X-Dispatcher: 1`. Read endpoints (`GET`) are always unrestricted.

### `GET /api/auto-managed`

Returns the current auto-managed state.

```json
{"enabled": true}
```

### `POST /api/auto-managed`

Set the auto-managed toggle. Body: `{"enabled": true}` or `{"enabled": false}`.

When `enabled` is `true`, all write endpoints (`/api/settings/storage`,
`/api/settings/tou/discharge/<N>`, `/api/discharge_all`) return HTTP 403 for requests
that do not carry the `X-Dispatcher: 1` header. The dispatcher always sends this header,
so its writes are unaffected.

```bash
# Enable auto-management (dispatcher takes over, UI is locked)
curl -X POST http://localhost:5000/api/auto-managed \
  -H "Content-Type: application/json" -d '{"enabled": true}'

# Disable (take manual control)
curl -X POST http://localhost:5000/api/auto-managed \
  -H "Content-Type: application/json" -d '{"enabled": false}'
```

---

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
