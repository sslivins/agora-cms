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
- **Never use native `confirm()`, `prompt()`, or `alert()` in the web UI.** Always use the custom modal helpers in `cms/static/app.js`: `showConfirm(message)`, `showPrompt(message, defaultValue)`, and `showToast(message, isError)`. `showConfirm` and `showPrompt` return Promises and must be `await`ed.
- **Never use the native `title` attribute for tooltips.** Always use the custom CSS tooltip: `<span class="has-tooltip">Label<span class="tooltip">Tooltip text</span></span>` (styled in `cms/static/style.css`).
- `cms/schemas/protocol.py` defines the shared CMS ↔ device message contract — **any changes here must be mirrored in the device repo** (`sslivins/agora`)
- API version lives in `cms/__init__.py` (`__version__`)
- **Always use 12-hour time format** (e.g. `2:30 PM`) in the web UI — both server-side (`strftime('%I:%M %p')`) and client-side (`hour12: true`). Never display 24-hour time to users.

## Bug Fixing — Test-Driven

- **Before fixing any bug, write a failing test that reproduces it.** Confirm the test fails, then implement the fix, then confirm the test passes.
- Tests live in `tests/` and use pytest + pytest-asyncio + httpx + aiosqlite.
- **For any web UI bug, also add a Playwright E2E test** in `tests_e2e/` that reproduces the issue in a real browser. E2E tests use Playwright + Chromium and run in CI alongside unit tests.

### Running Tests in Docker

Tests **must** run inside the Docker container (`agora-cms-cms-1`). The VS Code terminal tool's idle detection will prematurely kill long-running `docker exec` commands, so follow these patterns:

**Targeted tests (individual files — preferred for local verification):**
Run as a background terminal with `await_terminal` (timeout ≥ 60 000 ms). Individual test files finish in < 30 s:
```sh
docker exec agora-cms-cms-1 python -m pytest tests/test_foo.py -v --tb=short
```

**Full test suite (detached + sentinel poll):**
The full suite takes ~3–5 min. Use a detached exec so idle detection cannot kill it, then poll a sentinel file:
```sh
# 1. Clean up previous run
docker exec agora-cms-cms-1 rm -f /tmp/pytest_done /tmp/pytest_out.txt

# 2. Start tests detached
docker exec -d agora-cms-cms-1 sh -c \
  "python -m pytest tests/ --tb=short -q >/tmp/pytest_out.txt 2>&1; echo DONE >/tmp/pytest_done"

# 3. Poll until sentinel appears (run repeatedly)
docker exec agora-cms-cms-1 sh -c "cat /tmp/pytest_done 2>/dev/null || echo WAITING"

# 4. Read results once DONE
docker exec agora-cms-cms-1 tail -5 /tmp/pytest_out.txt
```

**Alternative — rely on CI:**
For full-suite regression checking, CI is often more reliable than local Docker polling. Run targeted tests locally, then let CI verify the full suite after pushing.

## Git Workflow

- **`main` is sacred** — never commit directly to `main`.
- All changes must be made on a feature branch and merged via pull request.
- Branch naming: `feat/<short-description>`, `fix/<short-description>`, `chore/<short-description>`, `perf/<short-description>`, `refactor/<short-description>`, `docs/<short-description>`, `test/<short-description>`, `ci/<short-description>`.
- **Never merge a PR** unless the user explicitly asks you to. Creating PRs is fine; merging requires explicit approval.
- Bump the version in `cms/__init__.py` when shipping user-facing changes.
- **After creating a PR, always check CI status** using `gh pr checks <number>` or `gh run list`. Monitor until all checks pass. If any fail, inspect the logs with `gh run view <run-id> --log-failed`, fix issues, push fixes, and re-check until green.

## Commit Messages — Conventional Commits

All commit messages **must** use [Conventional Commits](https://www.conventionalcommits.org/) format. The release workflow auto-generates changelogs from these prefixes.

**Format:** `<type>(<optional scope>): <description>`

| Prefix | When to use | Example |
|---|---|---|
| `feat:` | New feature or capability | `feat: add device group scheduling` |
| `fix:` | Bug fix | `fix: prevent stale device state on reconnect` |
| `perf:` | Performance improvement | `perf: batch WebSocket sync messages` |
| `refactor:` | Code restructuring (no behavior change) | `refactor: extract scheduler evaluation logic` |
| `test:` | Adding or updating tests only | `test: add E2E tests for schedule creation` |
| `docs:` | Documentation only | `docs: update protocol contract for v2` |
| `ci:` | CI/CD workflow changes | `ci: add changelog generation to release workflow` |
| `chore:` | Maintenance, deps, tooling | `chore: bump SQLAlchemy to 2.1` |

- Use the **imperative mood** in descriptions: "add" not "added", "fix" not "fixes".
- Optional scope in parentheses: `fix(scheduler): handle overlapping time windows`.
- Keep the first line under 72 characters.
- Add a blank line + body for complex changes.

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

Raspberry Pi Zero 2 W — ARM Cortex-A53, limited RAM/flash. CMS must be mindful of device constraints when distributing assets.
