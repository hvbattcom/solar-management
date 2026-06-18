# solis-management

HTTP API + web UI for writing Solis inverter settings over Modbus TCP.

Reads current state from holding registers and writes back via FC6/FC16.  
Companion to `solis-monitor/` (the read-only poller). Shares the same `config.cfg`.

---

## dispatcher.py — Solar plan dispatcher

Runs every 5 minutes via cron. Reads today's dispatch map (pushed by the solar planner),
derives the desired inverter state from past events, compares against live firmware, and
applies only what changed.

```cron
*/5 * * * * /usr/bin/python3 /path/to/dispatcher.py
```

Options:
```
--config PATH    path to config.cfg (default: ../solis-monitor/config.cfg)
--dry-run        print actions without writing to inverter
--time HH:MM     override current time (for testing)
--instance ID    load map for a specific instance_id
--show           print today's map as a colour timeline and exit
```

**Config** (`[SolisAPI]` section of `config.cfg`):

```ini
port = 5000                                    # solis-api port
mothership_prometheus_api = http://10.100.0.1/ # Prometheus for live SoC (optional)
```

### Dispatch map format

The solar planner writes a map to `maps/map_YYYY-MM-DD_{instance}.json`:

```json
{
  "date": "2026-06-19",
  "instance_id": "GS48",
  "algo": "optimal",
  "sell_kw": 30.0,
  "tou_slots": [
    {"start": "05:45", "end": "06:00", "amps": 100, "soc_floor_pct": 15},
    {"start": "20:00", "end": "22:00", "amps": 100, "soc_floor_pct": 23},
    {"start": "22:45", "end": "24:00", "amps": 100, "soc_floor_pct": 23}
  ],
  "events": [
    {"time": "05:45", "export": true,  "charge_amps": 1},
    {"time": "08:30", "export": false, "charge_amps": 100},
    {"time": "16:15", "export": true},
    {"time": "20:00", "export": true}
  ]
}
```

- **`tou_slots`** — battery discharge windows written to all 6 inverter TOU slots (padded with zeros).
- **`events`** — time-ordered state changes: `export` sets allow_export; `charge_amps` sets battery charge current.

### Behaviour per run

**Desired state** — walks all events with `time ≤ now + 3 min` and takes the last value seen
for each field (`export`, `charge_amps`). The 3-minute window absorbs cron jitter.

**TOU slot sync** — always compares all 6 firmware slots against the plan's `tou_slots`.
Writes a full 6-slot batch if any differ.  
Amps are split equally: `amps ÷ 2` written to both BAT1 and BAT2 (`/api/settings/battery`).  
Slot-to-window assignment is **randomised** on plan change to spread EEPROM wear across all
6 slot registers. The random order is persisted in `dispatcher_state.json` for the day.

**Export** — sets `allow_export` (and `max_export_power_w`) if it differs from firmware.

**Charge amps** — sets `bat1_charge_current_a = bat2_charge_current_a = charge_amps ÷ 2`
if it differs from firmware.

**Pending confirmation** — after each write, the written values are saved as `pending` in
state. On the next run they are verified against firmware:
- Confirmed → `pending` cleared, no retry.
- Not applied → logged as warning; the normal compare-and-apply loop retries automatically.

**SoC floor guard** — if the current time falls inside a `tou_slot` window and live SoC
(from Prometheus) ≤ `soc_floor_pct + 2%`, the active TOU slot is disabled immediately.
Requires `mothership_prometheus_api`; silently skipped if absent.

**`X-Dispatcher: 1` header** — all POST requests carry this header so solis-api can
distinguish dispatcher writes from manual UI writes when auto-management is ON.

### Show command

```bash
python3 dispatcher.py --show
python3 dispatcher.py --show --time 20:30
```

Prints a colour-coded timeline of today's map with the currently active event highlighted.

### State file (`dispatcher_state.json`)

```json
{
  "date": "2026-06-19",
  "plan_hash": "a3f1b2c4d5",
  "slot_order": [4, 2, 6, 1, 3, 5],
  "pending": {
    "export": true,
    "charge_amps": 1,
    "tou_slots": [...]
  }
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

- **Automatically managed toggle** (iOS-style switch, top of page) — when ON, the dispatcher
  controls the inverter; the rest of the page is greyed out and a banner is shown. Manual edits
  are blocked at the API level (the dispatcher bypasses this via `X-Dispatcher: 1`). The toggle
  auto-refreshes the page every 30 seconds while active. State is persisted in `auto_managed.json`.
- **Storage Settings** — mode selector (Self Use / Selling First / Off Grid), export limit, peak shaving power + enable, battery reserve, toggles for allow-export / allow-grid-charge / reserve-switch
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
    "max_export_power_w": 5000,
    "peak_shaving_on": false,
    "peak_shaving_power_w": 3000
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
| `mode` | string | `"Self Use"`, `"Time of Use"`, `"Off Grid"`, `"Selling First"` (UI exposes first three + Selling First) |
| `battery_reserve_on` | bool | `true` / `false` |
| `battery_reserve_pct` | int | 0 – 100 |
| `allow_export` | bool | `true` / `false` |
| `allow_grid_charge` | bool | `true` / `false` |
| `max_export_power_w` | int | ≥ 0, multiple of 100 |
| `peak_shaving_on` | bool | `true` / `false` — enables peak shaving (register 43483 bit 7) |
| `peak_shaving_power_w` | int | ≥ 0, multiple of 100 — grid import threshold above which battery limits charging |

```bash
# Switch to Selling First and set export limit
curl -X POST http://localhost:5000/api/settings/storage \
  -H "Content-Type: application/json" \
  -d '{"mode": "Selling First", "allow_export": true, "max_export_power_w": 10000}'

# Enable peak shaving at 3000 W grid import threshold
curl -X POST http://localhost:5000/api/settings/storage \
  -H "Content-Type: application/json" \
  -d '{"peak_shaving_on": true, "peak_shaving_power_w": 3000}'

# Just toggle grid charge off
curl -X POST http://localhost:5000/api/settings/storage \
  -H "Content-Type: application/json" \
  -d '{"allow_grid_charge": false}'
```

Response: `{"ok": true}` or `{"ok": false, "error": "..."}`

### `GET /api/settings/battery` and `POST /api/settings/battery`

Read or write BAT1 and BAT2 charge/discharge current limits.

```json
{
  "bat1_charge_current_a":    44.0,
  "bat1_discharge_current_a": 44.0,
  "bat2_charge_current_a":    44.0,
  "bat2_discharge_current_a": 44.0
}
```

Registers: BAT1 charge 43012, discharge 43013 (mirrored at 43117/43118); BAT2 charge 43804, discharge 43805.  
Scale: register value = amps × 10. Valid range: 1–200 A per battery.

```bash
# Throttle charge to 1 A per battery (maximise solar export)
curl -X POST http://localhost:5000/api/settings/battery \
  -H "Content-Type: application/json" \
  -d '{"bat1_charge_current_a": 1, "bat2_charge_current_a": 1}'

# Restore full charge (50 A per battery = 100 A total)
curl -X POST http://localhost:5000/api/settings/battery \
  -H "Content-Type: application/json" \
  -d '{"bat1_charge_current_a": 50, "bat2_charge_current_a": 50}'
```

---

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
| Mode / flags bitmask | 43110 | bit 0=Self Use, 1=TOU, 2=Off Grid, 4=Reserve On, 5=Grid Charge, 6=Selling First, 11=Peak Shaving mode |
| Hybrid function control | 43483 | bit 3=Allow Export (inverted: 0=allowed), bit 7=Peak Shaving enable |
| Peak Shaving power | 43488 | raw × 100 = W (100 W granularity) — grid import threshold |
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
