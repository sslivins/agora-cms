# Nightly E2E test suite

Full-stack end-to-end tests that run against a real `docker compose`
deployment of the CMS + worker + MCP + postgres + a Mailpit SMTP catcher
and the `agora-device-simulator` (sibling repo) running three simulated
devices.

Distinct from `tests_e2e/`, which runs the CMS in-process via uvicorn —
that suite is fast enough for PRs, but can't exercise the real container
topology, networking, postgres, or the SMTP round-trip.

## Prerequisites

- Docker 24+ with `docker compose` v2
- `agora-device-simulator` cloned as a sibling directory, with its
  `agora` submodule initialized:

  ```bash
  cd ..
  git clone git@github.com:sslivins/agora-device-simulator.git
  cd agora-device-simulator
  git submodule update --init --recursive
  ```

- Python test deps (shared with the normal test suite):

  ```bash
  pip install -r requirements-test.txt playwright httpx
  playwright install chromium
  ```

## Running locally

From the `agora-cms` repo root:

```bash
pytest tests/nightly --run-nightly -v
```

Or via env var:

```bash
NIGHTLY=1 pytest tests/nightly -v
```

The suite is opt-in — a plain `pytest` (or `pytest tests/`) will skip it
so regular dev workflows aren't slowed down by docker builds.

### Useful env vars

| Variable | Purpose | Default |
|---|---|---|
| `NIGHTLY_KEEP_STACK` | Leave the stack running after the session (for debugging). Manual `docker compose down -v` required after. | unset |
| `NIGHTLY_STARTUP_TIMEOUT` | Seconds to wait for CMS/mailpit/simulator healthchecks | 300 |
| `NIGHTLY_PROJECT` | `docker compose -p` name (lets multiple runs coexist) | `agora-nightly` |

### Debugging

- **Compose logs** are captured to `tests/nightly/last-run-logs.txt` on
  tear-down (success or failure).
- **Mailpit UI** is at http://127.0.0.1:8025 — browse captured emails.
- **Simulator control plane** is at http://127.0.0.1:9090 — e.g.
  `curl http://127.0.0.1:9090/devices`.
- Attach to a running stack:
  `docker compose -p agora-nightly -f docker-compose.yml -f tests/nightly/docker-compose.nightly.yml logs -f cms`

## What goes where?

| Destination | Use for |
|---|---|
| `tests/` | Unit tests; fast, in-process, no I/O beyond SQLite |
| `tests_e2e/` | Playwright against in-process uvicorn + `fake_device.py`. Per-PR. |
| `tests/nightly/` (this dir) | Full compose stack, real postgres, real SMTP, real simulator. Nightly cron. |

## Phases

See [#250](https://github.com/sslivins/agora-cms/issues/250) for the full
roadmap.

### Ordering philosophy

Phase numbers map to **layers in a dependency pyramid**, not to feature
priority. Each layer assumes the layers below work; a failure at layer N
will often produce noisy failures at layer N+1 unless layer N runs first
and fails loudly.

```
00          infra          docker compose stack comes up healthy
01          bootstrap      OOBE wizard seeds the first admin
01a         auth boundary  sub-second smoke: 401s enforced, built-in roles seeded, /me works
02-05       feature CRUD   assets, devices, groups, schedules — exercise each router as admin
06          governance     RBAC: operators/viewers scoped to groups, cross-group isolation
07          integrations   MCP: keys + /api/mcp/auth + tool round-trip
08          telemetry      thermal thresholds → dashboard banner → scoped notifications
```

The `01a_auth_smoke` phase is deliberately thin — it fires before any
feature phase so that a regression like "all `/api/*` leaked to anonymous
callers" or "built-in roles failed to seed" is caught in <1s with a clear
error message, rather than surfacing later as a confusing feature-CRUD
failure in Phase 02-05 or a permission-matrix failure in Phase 06.

### v1 phase list

| Phase | File | Purpose |
|---|---|---|
| 00 | `test_00_stack.py` | Stack health — CMS/mailpit/simulator ready |
| 01 | `test_01_oobe.py` | OOBE wizard via Playwright + Mailpit SMTP verification |
| 01a | `test_01a_auth_smoke.py` | Auth-boundary smoke: 401 walls, `/api/users/me`, built-in roles |
| 02 | `test_02_assets.py` | Asset upload + transcode variant validation |
| 03 | `test_03_devices.py` | Adopt simulated devices via UI |
| 04 | `test_04_groups.py` | Group CRUD + device assignment |
| 05 | `test_05_schedules.py` | Schedule → play → activity-log assertions |
| 06 | `test_06_rbac.py` | User profiles, welcome-email setup, permission matrix, cross-group isolation |
| 07 | `test_07_mcp.py` | MCP server: enable, key creation, `/api/mcp/auth`, tool round-trip |
| 08 | `test_08_thermal_notifications.py` | Thermal fault injection → dashboard banner → scoped notifications |
