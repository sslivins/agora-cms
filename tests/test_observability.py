"""Tests for cms.observability — App Insights bootstrap wrapper.

Issue #474, Phase 0 / A1.

These tests don't require ``azure-monitor-opentelemetry`` to be installed
in the test environment — we inject a fake module into ``sys.modules`` so
the wrapper's lazy import resolves to a controllable mock.
"""

from __future__ import annotations

import logging
import sys
import types
from unittest import mock

import pytest

from cms import observability


def _install_fake_azure_monitor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    configure: mock.MagicMock | None = None,
) -> mock.MagicMock:
    """Inject a stub ``azure.monitor.opentelemetry`` module.

    Returns the mock that ``configure_azure_monitor`` resolves to so
    tests can assert on it.
    """
    configure = configure or mock.MagicMock()

    azure_pkg = sys.modules.get("azure") or types.ModuleType("azure")
    monitor_pkg = sys.modules.get("azure.monitor") or types.ModuleType(
        "azure.monitor"
    )
    otel_pkg = types.ModuleType("azure.monitor.opentelemetry")
    otel_pkg.configure_azure_monitor = configure  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "azure", azure_pkg)
    monkeypatch.setitem(sys.modules, "azure.monitor", monitor_pkg)
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", otel_pkg)
    return configure


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch):
    """Clear the conn string env var and reset the init flag for each test."""
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("AGORA_CMS_DISABLE_OBSERVABILITY", raising=False)
    observability._reset_for_tests()
    yield
    observability._reset_for_tests()


def test_no_op_when_conn_string_unset(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="agora.cms.observability")

    result = observability.setup_observability()

    assert result is False
    assert any(
        "not set" in rec.message for rec in caplog.records
    ), "expected an informational log line about the env var being unset"


def test_no_op_when_conn_string_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "   ")

    assert observability.setup_observability() is False


def test_disable_env_var_short_circuits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The escape hatch must beat a present conn string."""
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000;",
    )
    monkeypatch.setenv("AGORA_CMS_DISABLE_OBSERVABILITY", "1")
    caplog.set_level(logging.INFO, logger="agora.cms.observability")
    mocked = _install_fake_azure_monitor(monkeypatch)

    result = observability.setup_observability()

    assert result is False
    mocked.assert_not_called()
    assert any(
        "disabled via" in rec.message for rec in caplog.records
    )


def test_calls_configure_azure_monitor_when_conn_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    conn = "InstrumentationKey=00000000-0000-0000-0000-000000000000;IngestionEndpoint=https://example/"
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", conn)
    caplog.set_level(logging.INFO, logger="agora.cms.observability")
    mocked = _install_fake_azure_monitor(monkeypatch)

    result = observability.setup_observability()

    assert result is True
    mocked.assert_called_once_with(connection_string=conn)
    assert any(
        "enabled" in rec.message for rec in caplog.records
    )


def test_idempotent_repeated_calls_only_init_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000;",
    )
    mocked = _install_fake_azure_monitor(monkeypatch)

    assert observability.setup_observability() is True
    assert observability.setup_observability() is True
    assert observability.setup_observability() is True

    mocked.assert_called_once()


def test_missing_sdk_does_not_crash(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If the SDK isn't installed, log a warning but never raise."""
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000;",
    )
    caplog.set_level(logging.WARNING, logger="agora.cms.observability")

    # Make sure the module is NOT in sys.modules — and block re-import.
    for name in (
        "azure.monitor.opentelemetry",
        "azure.monitor",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)

    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name.startswith("azure.monitor.opentelemetry"):
            raise ImportError("simulated missing SDK")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    result = observability.setup_observability()

    assert result is False
    assert any("not installed" in rec.message for rec in caplog.records)


def test_configure_failure_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: a misbehaving SDK init must not take down the CMS."""
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000;",
    )
    boom = mock.MagicMock(side_effect=RuntimeError("simulated boom"))
    _install_fake_azure_monitor(monkeypatch, configure=boom)

    # Must not raise.
    result = observability.setup_observability()

    assert result is False
    boom.assert_called_once()
