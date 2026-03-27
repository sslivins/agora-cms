# Agora CMS — Copilot Instructions

## Project Overview

Agora CMS is the central management server for a fleet of Agora media playback devices (Raspberry Pi Zero 2 W). It handles device registration, content scheduling, and asset distribution via a web UI and WebSocket API.

The companion device repo is [sslivins/agora](https://github.com/sslivins/agora).

## Architecture

Single application with a PostgreSQL database:

1. **REST API** — FastAPI endpoints for admin operations: device management, asset library, schedule CRUD.
2. **WebSocket endpoint** — Persistent connections from Agora devices. Handles registration, state sync, live push commands, and asset distribution coordination.
3. **Web UI** — Jinja2 server-rendered admin interface for managing devices, uploading content, and building schedules.
4. **Scheduler service** — Background task that evaluates schedules, determines what each device should play now and next, and triggers asset pre-fetch.

## Key Design Decisions

- **CMS is the single source of truth** — devices hold no persistent schedule. On reboot or reconnect, devices pull fresh state from the CMS.
- **Device-initiated WebSocket** — Devices connect outbound to the CMS, solving NAT/firewall issues. Works on LAN or cloud.
- **Flash-aware asset management** — At most 2 assets on a device at any time (current + next). CMS pre-fetches the next scheduled asset and cleans up old ones.
- **Schedules live in the CMS database** — Can be set far into the future. Assets are only transferred to devices when needed.
- **Protocol version** — All WebSocket messages include a protocol version. Must be kept in sync with the device-side implementation in `sslivins/agora`.

## Source Layout

- `cms/` — Main application package
  - `main.py` — FastAPI app entry point
  - `config.py` — Pydantic settings
  - `database.py` — SQLAlchemy engine and session
  - `models/` — SQLAlchemy ORM models (device, asset, schedule)
  - `schemas/` — Pydantic schemas for API and WebSocket protocol
  - `schemas/protocol.py` — **Shared contract** with device-side (WebSocket message types). Keep in sync with `sslivins/agora`.
  - `routers/` — FastAPI route handlers (devices, assets, schedules, WebSocket)
  - `services/` — Business logic (scheduler evaluation, asset distribution, device tracking)
  - `static/` — CSS, JS
  - `templates/` — Jinja2 admin UI templates

## Tech Stack

- **Python 3.11+**, **FastAPI**, **Pydantic v2**, **uvicorn**
- **PostgreSQL** + **SQLAlchemy 2.0** (async)
- **WebSocket** via FastAPI native support
- **Jinja2** for server-rendered admin UI
- **Docker Compose** for CMS + PostgreSQL

## Conventions

- Pydantic models for all API request/response schemas (`cms/schemas/`)
- SQLAlchemy ORM models for database tables (`cms/models/`)
- All WebSocket messages are JSON with a `type` field and `protocol_version`
- `cms/schemas/protocol.py` defines the shared CMS ↔ device message contract — **any changes here must be mirrored in the device repo** (`sslivins/agora`)
- API version lives in `cms/__init__.py` (`__version__`)
- Whenever API or WebSocket endpoints are added, changed, or removed, update `docs/openapi.yaml` to match.

## Git Workflow

- **`main` is sacred** — never commit directly to `main`.
- All changes must be made on a feature branch and merged via pull request.
- Branch naming: `feature/<short-description>`, `fix/<short-description>`, `chore/<short-description>`.
- Bump the version in `cms/__init__.py` when shipping user-facing changes.

## Protocol Contract (CMS ↔ Device)

Protocol version: **1**

### Device → CMS Messages
- `register` — Device ID, auth token, firmware version, storage capacity
- `status` — Heartbeat with playback state, disk usage, uptime
- `asset_ack` — Asset downloaded confirmation
- `asset_deleted` — Asset removed confirmation

### CMS → Device Messages
- `sync` — Full state push (schedule window, assigned assets, config)
- `play` — Immediate playback command
- `stop` — Stop playback, show splash
- `fetch_asset` — Instruct device to download an asset from URL
- `delete_asset` — Instruct device to remove a local asset
- `config` — Updated device configuration

## Data Model (Core Entities)

- **Device** — id (Pi serial/UUID), name, group, status (pending/approved/offline), last_seen, storage_capacity, firmware_version
- **DeviceGroup** — id, name, description (for bulk scheduling)
- **Asset** — id, filename, type (video/image), size, checksum, upload timestamp, stored on CMS
- **Schedule** — id, target (device or group), asset, start/end time, recurrence rule, priority, enabled
- **DeviceAsset** — tracks which assets are currently on which device (for flash-aware management)

## Hardware Target (Devices)

Raspberry Pi Zero 2 W — ARM Cortex-A53, limited RAM/flash. CMS must be mindful of device constraints when distributing assets. Design for ~30 concurrent device connections.
