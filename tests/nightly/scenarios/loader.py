"""Declarative scenario runner for the nightly suite (#301).

A scenario is a YAML file describing a sequence of steps executed against the
nightly compose stack: apply a simulator fault, wait for a notification,
assert an event row, etc.

The MVP supports the verbs already used by ``test_04_groups`` and
``test_08_thermal_notifications`` at the API level:

- ``fault``               - POST a fault (cpu_temp, codecs, ...) to a sim serial
- ``clear_fault``         - DELETE faults on a sim serial
- ``wait_for_notification`` - poll /api/notifications for a matching row
- ``expect_event``        - poll /api/devices/{id}/events for a matching row
- ``wait``                - plain sleep (debug aid)

Each scenario yields exactly one pytest item. Non-dev contributors can add a
smoke case by dropping a YAML file under ``tests/nightly/scenarios/``.
Complex cases that need custom assertions should stay in Python.

Schema:

    name: <str>                  # required, becomes pytest id
    description: <str>           # optional
    devices: [sim_a, sim_b]      # optional: simulator serials referenced below
    steps:
      - <verb>: { <args> }
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCENARIOS_DIR = Path(__file__).resolve().parent
SUPPORTED_VERBS = {
    "fault",
    "clear_fault",
    "wait_for_notification",
    "expect_event",
    "wait",
}


@dataclass
class Scenario:
    name: str
    path: Path
    description: str = ""
    devices: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)


class ScenarioValidationError(ValueError):
    """Raised when a YAML file does not match the minimal scenario schema."""


def _validate_step(step: Any, index: int, scenario_name: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(step, dict) or len(step) != 1:
        raise ScenarioValidationError(
            f"{scenario_name}: step {index} must be a mapping of one verb -> args, "
            f"got {step!r}"
        )
    verb, args = next(iter(step.items()))
    if verb not in SUPPORTED_VERBS:
        raise ScenarioValidationError(
            f"{scenario_name}: unknown verb {verb!r} at step {index}. "
            f"Supported: {sorted(SUPPORTED_VERBS)}"
        )
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ScenarioValidationError(
            f"{scenario_name}: step {index} ({verb}) args must be a mapping, got {args!r}"
        )
    return verb, args


def load_scenario(path: Path) -> Scenario:
    """Parse and validate a single scenario YAML file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ScenarioValidationError(f"{path}: top-level must be a mapping")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ScenarioValidationError(f"{path}: 'name' is required and must be a non-empty string")
    steps = raw.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise ScenarioValidationError(f"{path}: 'steps' must be a non-empty list")
    for i, step in enumerate(steps):
        _validate_step(step, i, name)
    devices = raw.get("devices") or []
    if not isinstance(devices, list) or not all(isinstance(d, str) for d in devices):
        raise ScenarioValidationError(f"{path}: 'devices' must be a list of strings")
    return Scenario(
        name=name.strip(),
        path=path,
        description=raw.get("description", "") or "",
        devices=list(devices),
        steps=list(steps),
    )


def discover_scenarios(root: Path | None = None) -> list[Scenario]:
    """Return every scenario YAML found under ``root`` (default: this dir)."""
    root = root or SCENARIOS_DIR
    scenarios: list[Scenario] = []
    for path in sorted(root.glob("*.yaml")):
        scenarios.append(load_scenario(path))
    return scenarios


# -- step executors --------------------------------------------------------
#
# Each executor takes the verb args plus a runtime context dict with
# ``page`` (authenticated_page), ``simulator`` (SimulatorClient) and a
# scratch ``state`` dict (e.g. resolved device ids).


def _resolve_device_id(page: Any, serial: str) -> str | None:
    """Look up the CMS device UUID for a simulator serial."""
    resp = page.request.get("/api/devices")
    if resp.status != 200:
        return None
    for dev in resp.json():
        if dev.get("serial") == serial:
            return dev.get("id")
    return None


def _step_fault(args: dict[str, Any], ctx: dict[str, Any]) -> None:
    target = args.get("target")
    if not target:
        raise ScenarioValidationError("'fault' requires 'target'")
    fault_kwargs = {k: v for k, v in args.items() if k != "target"}
    ctx["simulator"].apply_fault(target, **fault_kwargs)


def _step_clear_fault(args: dict[str, Any], ctx: dict[str, Any]) -> None:
    target = args.get("target")
    if not target:
        raise ScenarioValidationError("'clear_fault' requires 'target'")
    ctx["simulator"].clear_faults(target)


def _step_wait_for_notification(args: dict[str, Any], ctx: dict[str, Any]) -> None:
    device_serial = args.get("device")
    notif_type = args.get("type")
    timeout = float(args.get("timeout", 15.0))
    if not device_serial or not notif_type:
        raise ScenarioValidationError(
            "'wait_for_notification' requires 'device' and 'type'"
        )
    page = ctx["page"]
    dev_id = _resolve_device_id(page, device_serial)
    if dev_id is None:
        raise AssertionError(f"no CMS device registered for simulator serial {device_serial!r}")
    deadline = time.monotonic() + timeout
    last_seen: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        resp = page.request.get("/api/notifications/")
        if resp.status == 200:
            body = resp.json()
            items = body if isinstance(body, list) else body.get("items", [])
            last_seen = items
            for n in items:
                if n.get("device_id") == dev_id and n.get("type") == notif_type:
                    return
        time.sleep(1.0)
    raise AssertionError(
        f"no notification of type={notif_type!r} for device={device_serial!r} "
        f"within {timeout}s (last seen types: {[n.get('type') for n in last_seen]})"
    )


def _step_expect_event(args: dict[str, Any], ctx: dict[str, Any]) -> None:
    device_serial = args.get("device")
    event_type = args.get("type")
    timeout = float(args.get("timeout", 15.0))
    if not device_serial or not event_type:
        raise ScenarioValidationError("'expect_event' requires 'device' and 'type'")
    page = ctx["page"]
    dev_id = _resolve_device_id(page, device_serial)
    if dev_id is None:
        raise AssertionError(f"no CMS device registered for simulator serial {device_serial!r}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = page.request.get(f"/api/devices/{dev_id}/events")
        if resp.status == 200:
            body = resp.json()
            events = body if isinstance(body, list) else body.get("items", [])
            for evt in events:
                if evt.get("type") == event_type or evt.get("event_type") == event_type:
                    return
        time.sleep(1.0)
    raise AssertionError(
        f"no event of type={event_type!r} for device={device_serial!r} within {timeout}s"
    )


def _step_wait(args: dict[str, Any], ctx: dict[str, Any]) -> None:
    seconds = float(args.get("seconds", 0))
    time.sleep(seconds)


STEP_EXECUTORS = {
    "fault": _step_fault,
    "clear_fault": _step_clear_fault,
    "wait_for_notification": _step_wait_for_notification,
    "expect_event": _step_expect_event,
    "wait": _step_wait,
}


def run_scenario(scenario: Scenario, *, page: Any, simulator: Any) -> None:
    """Execute the steps of a scenario, raising on the first failure."""
    ctx = {"page": page, "simulator": simulator, "state": {}}
    for i, step in enumerate(scenario.steps):
        verb, args = _validate_step(step, i, scenario.name)
        executor = STEP_EXECUTORS[verb]
        try:
            executor(args, ctx)
        except AssertionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"scenario {scenario.name!r} step {i} ({verb}) raised: {exc!r}"
            ) from exc
