# Nightly scenarios

Drop a `*.yaml` file in this directory and it becomes a pytest test under
`tests/nightly/test_99_scenarios.py::test_scenario[<name>]`.

The loader (`loader.py`) validates each scenario's schema and executes its
steps against the running compose stack. Complex cases that need custom
Python belong in their own `test_XX_*.py` module instead.

## Schema

```yaml
name: <string>               # required; becomes the pytest id
description: <string>        # optional
devices: [<serial>, ...]     # optional documentation-only list of sim serials
steps:
  - <verb>: { <args> }       # one verb per step, keyed mapping
  - <verb>: { <args> }
```

## Verbs (MVP)

| Verb | Args | Effect |
|---|---|---|
| `fault` | `target: <serial>`, any sim fault kwargs (e.g. `cpu_temp: 82`) | POSTs to the simulator control plane |
| `clear_fault` | `target: <serial>` | DELETEs active faults on a serial |
| `wait_for_notification` | `device: <serial>`, `type: <notif type>`, `timeout: <sec, default 15>` | Polls `/api/notifications/` until a matching row appears |
| `expect_event` | `device: <serial>`, `type: <event type>`, `timeout: <sec, default 15>` | Polls `/api/devices/{id}/events` until a matching row appears |
| `wait` | `seconds: <float>` | Plain sleep (debug aid — don't use for real synchronization) |

## Ordering inside the suite

Scenarios run late (`test_99_*`), after the OOBE wizard, device adoption, and
RBAC setup have all completed. They rely on the `authenticated_page` and
`simulator` fixtures from `conftest.py`.

## Adding new verbs

1. Add the verb name to `SUPPORTED_VERBS` in `loader.py`.
2. Implement a `_step_<verb>(args, ctx)` executor and register it in
   `STEP_EXECUTORS`.
3. Add a unit test in `tests/test_nightly_scenario_loader.py`.
