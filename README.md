# Agora CMS — Central Management System

Agora CMS is the central control server for a fleet of [Agora](https://github.com/sslivins/agora) media playback devices (Raspberry Pi Zero 2 W). It manages device registration, content scheduling, and asset distribution across up to ~30 devices on a network.

## How It Works

```
┌─────────────┐         WebSocket (device-initiated)         ┌─────────────┐
│  Agora CMS  │◄────────────────────────────────────────────►│  RPi Device  │
│  (FastAPI)  │  register → sync schedule → push updates     │  (agora)     │
│  PostgreSQL │  ◄── status heartbeats ──                    │  GStreamer   │
│  Web UI     │  ── asset download URL ──►                   │  Player      │
└─────────────┘                                              └─────────────┘
       ▲                                                           ×30
       │ Browser
  ┌─────────┐
  │  Admin   │  Manage devices, upload content, build schedules
  │  Web UI  │
  └─────────┘
```

### Device Communication

- **Device-initiated WebSocket**: Each Pi connects outbound to the CMS on boot, solving NAT/firewall issues. Works whether the CMS is on the local network or in the cloud.
- **On connect**: Device authenticates with its unique ID + token, pulls its current schedule and asset assignments.
- **Live updates**: CMS pushes changes instantly over the open WebSocket — schedule updates, "play now" overrides, new asset notifications.
- **On disconnect**: Device reconnects and re-syncs automatically. No local schedule persistence — the CMS is the single source of truth.

### Device Registration

1. New device boots and connects to the CMS WebSocket endpoint
2. CMS sees an unknown device ID → creates it as **pending**
3. Admin approves the device in the CMS web UI, assigns it to a group/location
4. Device receives its config (splash screen, schedule, settings)

### Flash-Aware Asset Management

Raspberry Pi devices have limited SD card storage. The CMS manages this:

- **At most 2 videos on a device at any time** — the currently playing asset and the next scheduled one
- Schedules can be set far into the future (e.g., a Christmas video created in summer) but assets are only transferred to the device when needed
- CMS pre-fetches the next asset before its start time
- When an asset is no longer needed, CMS instructs the device to delete it
- Supports devices with varying storage capacity (minimum: 1 video)

### Scheduling

- **One-time**: Play video X on device Y from 2pm–4pm on Dec 25
- **Recurring**: Play video X every Mon/Wed/Fri 11am–2pm
- **Default/fallback**: What to show when nothing is scheduled (splash screen or a default video)
- **Priority**: Emergency content can override the regular schedule
- Schedules are per-device or per-device-group

## Tech Stack

- **Python 3.11+**, **FastAPI**, **Pydantic v2**, **uvicorn**
- **PostgreSQL** — devices, groups, schedules, asset metadata
- **WebSocket** — real-time device communication (FastAPI native)
- **Jinja2** — server-rendered admin web UI
- **Docker Compose** — CMS + PostgreSQL

## Project Structure

```
cms/
  __init__.py          # version
  main.py              # FastAPI app entry point
  config.py            # Settings (Pydantic)
  database.py          # SQLAlchemy / DB session
  models/              # SQLAlchemy ORM models
    device.py          # Device, DeviceGroup
    asset.py           # Asset metadata
    schedule.py        # Schedule, ScheduleRule
  schemas/             # Pydantic request/response schemas
    device.py
    asset.py
    schedule.py
    protocol.py        # WebSocket message types (shared contract with device)
  routers/
    devices.py         # Device management API
    assets.py          # Asset library API
    schedules.py       # Schedule CRUD API
    ws.py              # WebSocket endpoint for devices
  services/
    scheduler.py       # Schedule evaluation (what should play now/next)
    asset_manager.py   # Asset distribution logic
    device_manager.py  # Device state tracking, connection registry
  static/              # CSS, JS
  templates/           # Jinja2 admin UI templates
```

## Protocol (CMS ↔ Device)

Protocol version: **1**

All WebSocket messages are JSON with a `type` field:

### Device → CMS
| Type | Description |
|------|-------------|
| `register` | Initial handshake: device ID, auth token, firmware version, storage capacity |
| `status` | Periodic heartbeat: current playback state, disk usage, uptime |
| `asset_ack` | Confirms asset downloaded successfully |
| `asset_deleted` | Confirms asset removed from local storage |

### CMS → Device
| Type | Description |
|------|-------------|
| `sync` | Full state: current schedule window, assigned assets, config |
| `play` | Immediate playback command (asset name, loop flag) |
| `stop` | Stop playback, show splash |
| `fetch_asset` | Download an asset from URL (pre-fetch for upcoming schedule) |
| `delete_asset` | Remove an asset from local storage |
| `config` | Updated device config (splash, settings) |

## Related

- **[agora](https://github.com/sslivins/agora)** — Device-side media player for Raspberry Pi Zero 2 W
