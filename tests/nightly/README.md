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
roadmap. v1 phases:

1. **Phase 1 (this PR)** — harness + sanity test that the stack comes up
2. Phase 2 — OOBE wizard via Playwright with Mailpit SMTP verification
3. Phase 3 — asset upload + transcode variant validation
4. Phase 4 — adopt 3 simulated devices via UI
5. Phase 5 — groups
6. Phase 6 — schedule → play → verify device messages + activity log
