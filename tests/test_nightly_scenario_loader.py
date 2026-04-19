"""Unit tests for the nightly scenario YAML loader (#301).

These run in the normal unit test job — no compose stack required.
They validate that:

* well-formed YAML scenarios parse into Scenario objects,
* the shipped example scenario stays syntactically valid,
* malformed scenarios raise ScenarioValidationError with a useful message,
* every step executor can be invoked against a minimal fake runtime
  (page + simulator) without tripping over the argument contract.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from tests.nightly.scenarios import loader


SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "tests" / "nightly" / "scenarios"


# ── schema parsing ────────────────────────────────────────────────────────


def _write_yaml(tmp_path: Path, doc: dict) -> Path:
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def test_shipped_example_scenario_parses():
    path = SCENARIOS_DIR / "thermal_round_trip_api.yaml"
    assert path.exists(), f"shipped example missing at {path}"
    scenario = loader.load_scenario(path)
    assert scenario.name == "thermal_round_trip_api"
    assert scenario.steps
    assert {next(iter(step)) for step in scenario.steps} <= loader.SUPPORTED_VERBS


def test_load_minimal_scenario(tmp_path):
    path = _write_yaml(tmp_path, {
        "name": "minimal",
        "steps": [{"wait": {"seconds": 0}}],
    })
    s = loader.load_scenario(path)
    assert s.name == "minimal"
    assert s.description == ""
    assert s.devices == []
    assert len(s.steps) == 1


def test_discover_scenarios_finds_shipped_yaml():
    scenarios = loader.discover_scenarios(SCENARIOS_DIR)
    names = {s.name for s in scenarios}
    assert "thermal_round_trip_api" in names


def test_discover_scenarios_in_empty_dir(tmp_path):
    assert loader.discover_scenarios(tmp_path) == []


# ── schema rejection ──────────────────────────────────────────────────────


def test_rejects_missing_name(tmp_path):
    path = _write_yaml(tmp_path, {"steps": [{"wait": {"seconds": 0}}]})
    with pytest.raises(loader.ScenarioValidationError, match="'name' is required"):
        loader.load_scenario(path)


def test_rejects_empty_steps(tmp_path):
    path = _write_yaml(tmp_path, {"name": "x", "steps": []})
    with pytest.raises(loader.ScenarioValidationError, match="'steps' must be a non-empty list"):
        loader.load_scenario(path)


def test_rejects_unknown_verb(tmp_path):
    path = _write_yaml(tmp_path, {
        "name": "x",
        "steps": [{"nuke_prod": {}}],
    })
    with pytest.raises(loader.ScenarioValidationError, match="unknown verb 'nuke_prod'"):
        loader.load_scenario(path)


def test_rejects_step_with_multiple_verbs(tmp_path):
    # A step must map exactly one verb → args; two keys are ambiguous.
    path = tmp_path / "s.yaml"
    path.write_text(
        "name: x\nsteps:\n  - {fault: {target: a}, wait: {seconds: 1}}\n",
        encoding="utf-8",
    )
    with pytest.raises(loader.ScenarioValidationError, match="mapping of one verb"):
        loader.load_scenario(path)


def test_rejects_non_mapping_args(tmp_path):
    path = _write_yaml(tmp_path, {
        "name": "x",
        "steps": [{"wait": 5}],  # args must be a mapping
    })
    with pytest.raises(loader.ScenarioValidationError, match="args must be a mapping"):
        loader.load_scenario(path)


def test_rejects_non_list_devices(tmp_path):
    path = _write_yaml(tmp_path, {
        "name": "x",
        "devices": "sim-000000",
        "steps": [{"wait": {"seconds": 0}}],
    })
    with pytest.raises(loader.ScenarioValidationError, match="'devices' must be a list"):
        loader.load_scenario(path)


# ── step executors ────────────────────────────────────────────────────────


def _fake_page_with_device(serial: str, device_id: str):
    """Build a minimal authenticated_page-alike for notification/event tests."""
    page = MagicMock()

    def _get(path: str, **_kw):
        if path == "/api/devices":
            return SimpleNamespace(
                status=200,
                json=lambda: [{"serial": serial, "id": device_id}],
            )
        if path == "/api/notifications/":
            return SimpleNamespace(
                status=200,
                json=lambda: [{"device_id": device_id, "type": "temp_high"}],
            )
        if path.endswith("/events"):
            return SimpleNamespace(
                status=200,
                json=lambda: [{"type": "TEMP_HIGH"}],
            )
        return SimpleNamespace(status=404, json=lambda: {})

    page.request.get.side_effect = _get
    return page


def test_step_fault_calls_apply_fault():
    simulator = MagicMock()
    ctx = {"page": MagicMock(), "simulator": simulator, "state": {}}
    loader._step_fault({"target": "sim-0", "cpu_temp": 82}, ctx)
    simulator.apply_fault.assert_called_once_with("sim-0", cpu_temp=82)


def test_step_fault_requires_target():
    with pytest.raises(loader.ScenarioValidationError, match="requires 'target'"):
        loader._step_fault({"cpu_temp": 82}, {"page": MagicMock(), "simulator": MagicMock(), "state": {}})


def test_step_clear_fault_calls_simulator():
    simulator = MagicMock()
    loader._step_clear_fault(
        {"target": "sim-0"},
        {"page": MagicMock(), "simulator": simulator, "state": {}},
    )
    simulator.clear_faults.assert_called_once_with("sim-0")


def test_step_wait_for_notification_resolves_device_and_matches():
    page = _fake_page_with_device("sim-0", "dev-uuid")
    ctx = {"page": page, "simulator": MagicMock(), "state": {}}
    # Must not raise.
    loader._step_wait_for_notification(
        {"device": "sim-0", "type": "temp_high", "timeout": 2},
        ctx,
    )


def test_step_wait_for_notification_times_out_on_wrong_type():
    page = _fake_page_with_device("sim-0", "dev-uuid")
    ctx = {"page": page, "simulator": MagicMock(), "state": {}}
    with pytest.raises(AssertionError, match="no notification of type='temp_cleared'"):
        loader._step_wait_for_notification(
            {"device": "sim-0", "type": "temp_cleared", "timeout": 1},
            ctx,
        )


def test_step_wait_for_notification_rejects_unknown_serial():
    page = MagicMock()
    page.request.get.return_value = SimpleNamespace(status=200, json=lambda: [])
    with pytest.raises(AssertionError, match="no CMS device registered"):
        loader._step_wait_for_notification(
            {"device": "ghost", "type": "temp_high", "timeout": 1},
            {"page": page, "simulator": MagicMock(), "state": {}},
        )


def test_step_expect_event_matches():
    page = _fake_page_with_device("sim-0", "dev-uuid")
    loader._step_expect_event(
        {"device": "sim-0", "type": "TEMP_HIGH", "timeout": 2},
        {"page": page, "simulator": MagicMock(), "state": {}},
    )


def test_step_wait_sleeps_for_seconds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(loader.time, "sleep", lambda s: sleeps.append(s))
    loader._step_wait({"seconds": 0.25}, {"page": MagicMock(), "simulator": MagicMock(), "state": {}})
    assert sleeps == [0.25]


# ── end-to-end runner ────────────────────────────────────────────────────


def test_run_scenario_executes_all_steps(monkeypatch):
    monkeypatch.setattr(loader.time, "sleep", lambda _s: None)
    simulator = MagicMock()
    page = _fake_page_with_device("sim-0", "dev-uuid")

    scenario = loader.Scenario(
        name="inline",
        path=Path("<memory>"),
        steps=[
            {"fault": {"target": "sim-0", "cpu_temp": 82}},
            {"wait_for_notification": {"device": "sim-0", "type": "temp_high", "timeout": 1}},
            {"expect_event": {"device": "sim-0", "type": "TEMP_HIGH", "timeout": 1}},
            {"clear_fault": {"target": "sim-0"}},
            {"wait": {"seconds": 0}},
        ],
    )
    loader.run_scenario(scenario, page=page, simulator=simulator)
    simulator.apply_fault.assert_called_once()
    simulator.clear_faults.assert_called_once_with("sim-0")


def test_run_scenario_wraps_unexpected_exceptions(monkeypatch):
    simulator = MagicMock()
    simulator.apply_fault.side_effect = RuntimeError("boom")
    scenario = loader.Scenario(
        name="oops",
        path=Path("<memory>"),
        steps=[{"fault": {"target": "sim-0"}}],
    )
    with pytest.raises(AssertionError, match=r"step 0 \(fault\) raised"):
        loader.run_scenario(scenario, page=MagicMock(), simulator=simulator)
