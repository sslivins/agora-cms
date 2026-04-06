# Agora CMS — Central Management Server

**Manage a fleet of digital signage displays from a single dashboard.**

Agora CMS is the command center for [Agora](https://github.com/sslivins/agora) media playback devices running on Raspberry Pi Zero 2 W. Upload content once, build time-based schedules, and the CMS handles the rest — transcoding video for each device's hardware, distributing assets when needed, and pushing live updates over WebSocket. No manual device configuration required.

**One server. Dozens of screens. Fully self-hosted.**

### Highlights

- **Plug-and-play device management** — Devices auto-register over WebSocket; approve them from the dashboard and they start playing
- **Smart scheduling** — Time-based, date-ranged, recurring schedules with priority levels and per-device or per-group targeting
- **Automatic video transcoding** — Uploads are transcoded to hardware-friendly H.264 for each device profile using ffmpeg
- **Flash-aware asset distribution** — CMS tracks each device's SD card budget, pre-fetches upcoming assets, and cleans up old ones
- **Real-time fleet monitoring** — Live playback state, CPU temperature, storage usage, firmware versions, and error reporting
- **Remote control** — Play, stop, reboot, factory reset, toggle SSH, push config changes — all from the web UI
- **Zero-touch updates** — Docker image auto-updates via Watchtower; device firmware upgrades pushed over-the-air

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

## Quick Start

```bash
cp .env.example .env    # Edit credentials and secrets
docker compose up -d    # Starts CMS + PostgreSQL
```

The web UI is available at `http://localhost:8080`. Default login: `admin` / `agora`.

## Production Deployment (VM)

For deploying on a Linux VM with automatic updates via [Watchtower](https://containrrr.dev/watchtower/):

```bash
curl -fsSL https://raw.githubusercontent.com/sslivins/agora-cms/main/setup.sh | bash
```

Or step by step:

```bash
# 1. Download the setup script
curl -fsSL https://raw.githubusercontent.com/sslivins/agora-cms/main/setup.sh -o setup.sh
chmod +x setup.sh

# 2. Run it (installs Docker if needed, downloads compose file, creates .env)
./setup.sh              # default: /opt/agora-cms
./setup.sh /srv/cms     # or specify a custom directory

# 3. Edit .env with real credentials
nano /opt/agora-cms/.env

# 4. Restart with final config
cd /opt/agora-cms && docker compose up -d
```

The production compose file (`docker-compose.prod.yml`) pulls the pre-built image from `ghcr.io/sslivins/agora-cms:latest` instead of building locally. Watchtower checks for new images every 5 minutes and restarts the CMS container automatically.

### Updating

Updates happen automatically. When a new commit is pushed to `main`, GitHub Actions builds and publishes a new Docker image. Watchtower detects the change and restarts the CMS container with zero manual intervention.

To update manually or check status:

```bash
cd /opt/agora-cms
docker compose pull cms      # pull latest image
docker compose up -d         # restart with new image
docker compose logs -f watchtower  # check watchtower logs
```

## Features

### Device Management

- **Auto-registration**: New devices connecting are created as **pending** for admin approval
- **Device groups**: Organize devices by location or purpose for bulk scheduling
- **Device profiles**: Hardware capability templates (codec, resolution, bitrate) for transcoding
- **Live status**: See each device's playback state, uptime, and storage in real time
- **Remote control**: Play, stop, reboot, set password, toggle SSH, and push config updates
- **Reset Auth**: Clear auth credentials for re-flashed devices without database access
- **API key rotation**: Device API keys are automatically rotated on a configurable interval

### Content & Asset Library

- **Upload**: Drag-and-drop media upload (up to 2 GB) with automatic format detection
- **Image conversion**: HEIC, AVIF, WebP, BMP, TIFF, GIF are auto-converted to JPEG on upload
- **Video transcoding**: Videos are automatically transcoded for each device profile using ffmpeg
  - Hardware-friendly: H.264 Main profile, BT.709 color space (Pi V4L2 compatible)
  - Scale-to-fit with aspect ratio preservation
  - Progress tracking in the UI
  - UUID-based variant filenames to avoid collisions
- **Media metadata**: Resolution, duration, codecs, bitrate, frame rate extracted via ffprobe
- **Preview**: Stream source files directly from the library

### Scheduling

- **Time-based**: Play asset X on device Y from 2pm–4pm
- **Date ranges**: Start/end dates for seasonal content (e.g., holiday videos)
- **Recurring**: Select days of the week (Mon/Wed/Fri, weekdays, etc.)
- **Default/fallback**: What to show when nothing is scheduled (per-device or per-group)
- **Priority**: Higher-priority schedules override lower ones
- **End Now**: Skip the current occurrence of a schedule immediately
- **Per-device or per-group**: Target individual devices or entire groups

### Flash-Aware Asset Distribution

- Devices have limited SD card storage — CMS manages this automatically
- Assets are only transferred when needed for upcoming schedules
- CMS pre-fetches the next asset before its start time
- When an asset is no longer needed, CMS instructs the device to delete it
- Budget-aware: respects each device's reported storage capacity

## Web UI Pages

| Page | Description |
|------|-------------|
| Dashboard | Device status overview, now-playing, upcoming schedules |
| Devices | Device list with inline editing, groups management, remote actions |
| Assets | Upload, library browser, variant/transcoding status, preview |
| Schedules | Schedule table with create/edit modals, end-now |
| Profiles | Device profiles for transcoding (built-in Pi Zero 2 W + custom) |
| Settings | Admin password, timezone configuration |

## API

The full REST API is documented in [docs/openapi.yaml](docs/openapi.yaml). You can explore it interactively using the [Swagger Editor](https://editor.swagger.io/?url=https://raw.githubusercontent.com/sslivins/agora-cms/main/docs/openapi.yaml).

## Protocol (CMS ↔ Device)

Protocol version: **1**

All WebSocket messages are JSON with a `type` field. Full schema in [cms/schemas/protocol.py](cms/schemas/protocol.py).

### Device → CMS

| Type | Description |
|------|-------------|
| `register` | Device ID, auth token, firmware version, storage capacity |
| `status` | Heartbeat: playback state, disk usage, uptime, CPU temp (every 30s) |
| `fetch_request` | Request an asset by filename |
| `fetch_failed` | Download failed with reason and budget info |
| `asset_ack` | Confirm asset downloaded with checksum |
| `asset_deleted` | Confirm asset removed |

### CMS → Device

| Type | Description |
|------|-------------|
| `auth_assigned` | Initial auth token (new device registration) |
| `sync` | Full schedule window, timezone, default asset |
| `play` | Immediate playback command |
| `stop` | Stop playback |
| `fetch_asset` | Download URL + checksum + size |
| `delete_asset` | Remove local asset |
| `config` | Update splash, password, API key, device name, SSH access |
| `reboot` | Reboot device |
| `upgrade` | Trigger firmware upgrade |

### Connection Flow

1. Device sends `register` with ID + auth token (empty on first connect)
2. CMS creates device as **pending** if new
3. CMS sends `auth_assigned` with unique token (stored hashed)
4. CMS sends `sync` with schedule window
5. CMS sends `fetch_asset` + `play` for approved devices with default asset
6. CMS sends `config` with rotated API key
7. Ongoing: device `status` heartbeats, CMS pushes schedule changes

## Tech Stack

- **Python 3.11+**, **FastAPI**, **Pydantic v2**, **uvicorn**
- **PostgreSQL 16** + **SQLAlchemy 2.0** (async with asyncpg)
- **ffmpeg** / **ffprobe** for video transcoding and metadata
- **libheif** for HEIC image conversion
- **WebSocket** — real-time device communication (FastAPI native)
- **Jinja2** — server-rendered admin web UI
- **Docker Compose** — CMS + PostgreSQL

## Configuration

Environment variables (prefix `AGORA_CMS_`), set in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://agora:agora@db:5432/agora_cms` | PostgreSQL connection |
| `SECRET_KEY` | `change-me-in-production` | Session signing key |
| `ADMIN_USERNAME` | `admin` | Initial admin username |
| `ADMIN_PASSWORD` | `agora` | Initial admin password |
| `ASSET_STORAGE_PATH` | `/opt/agora-cms/assets` | Asset storage root |
| `DEFAULT_DEVICE_STORAGE_MB` | `500` | Default device flash budget |
| `API_KEY_ROTATION_HOURS` | `24` | Device API key rotation interval |
| `PENDING_DEVICE_TTL_HOURS` | `24` | Auto-purge pending devices not seen for N hours |

## Project Structure

```
cms/
  __init__.py              # Version
  main.py                  # FastAPI app, startup migrations, background tasks
  config.py                # Pydantic settings
  auth.py                  # Session auth, password hashing
  database.py              # SQLAlchemy engine and session
  ui.py                    # Web UI routes (Jinja2)
  models/
    device.py              # Device, DeviceGroup, DeviceProfile
    asset.py               # Asset, AssetVariant, DeviceAsset
    schedule.py            # Schedule
    setting.py             # CMSSetting (admin credentials, timezone)
  schemas/
    device.py              # Device CRUD schemas
    asset.py               # Asset response schemas
    schedule.py            # Schedule CRUD schemas
    profile.py             # Profile CRUD schemas (name validation, immutable on update)
    protocol.py            # WebSocket message types (shared contract with device repo)
  routers/
    devices.py             # Device management API
    assets.py              # Asset library API
    schedules.py           # Schedule CRUD API
    profiles.py            # Device profile API
    ws.py                  # WebSocket endpoint
  services/
    scheduler.py           # Schedule evaluation, sync pushing
    transcoder.py          # Video transcoding, media probing
    device_manager.py      # Connection registry, state tracking
    version_checker.py     # Firmware version polling
    device_purge.py        # Stale pending device cleanup
  static/                  # CSS, JS
  templates/               # Jinja2 admin UI templates
scripts/
  variant-lookup.sh        # SSH debugging tool for variant queries
tests/                     # pytest + pytest-asyncio + httpx + aiosqlite
tests_e2e/                 # Playwright E2E browser tests
```

## Data Model

| Table | Purpose |
|-------|---------|
| `devices` | Device registry (ID, name, status, group, profile, storage, auth hashes) |
| `device_groups` | Groups for bulk scheduling |
| `device_profiles` | Hardware capability templates for transcoding |
| `assets` | Source media library with metadata |
| `asset_variants` | Transcoded formats per device profile (UUID filenames, status, progress) |
| `device_assets` | Tracks which assets are on which device |
| `schedules` | Schedule rules (target, asset, time window, recurrence, priority) |
| `cms_settings` | Runtime settings (admin credentials, timezone) |

## Resetting the Admin Password

If you lose the admin password:

1. Edit `.env` and uncomment the reset line:
   ```
   AGORA_CMS_RESET_PASSWORD=true
   ```
   The password will be reset to whatever `AGORA_CMS_ADMIN_PASSWORD` is set to in `.env`.

2. Restart the CMS:
   ```bash
   docker compose restart cms
   ```

3. Log in with the password from `.env`, then **comment the line back out** and restart:
   ```
   # AGORA_CMS_RESET_PASSWORD=true
   ```
   ```bash
   docker compose restart cms
   ```

## Development

### Running Tests

```bash
docker exec agora-cms-cms-1 python -m pytest tests/ --tb=short -q
```

### Rebuilding

```bash
docker compose up -d --build
```

## Related

- **[agora](https://github.com/sslivins/agora)** — Device-side media player for Raspberry Pi Zero 2 W
