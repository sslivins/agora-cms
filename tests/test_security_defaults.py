"""Unit tests for cms.security (#308)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from cms.security import (
    SecretFinding,
    detect_default_secrets,
    warn_on_default_secrets,
)


def _settings(**overrides):
    base = {"secret_key": "real-random-32-chars-xxxxxxxxxxxxx", "admin_password": "a-real-password"}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_detect_no_defaults_returns_empty():
    assert detect_default_secrets(_settings()) == []


def test_detect_default_secret_key():
    findings = detect_default_secrets(_settings(secret_key="change-me-in-production"))
    assert len(findings) == 1
    f = findings[0]
    assert isinstance(f, SecretFinding)
    assert f.setting == "secret_key"
    assert f.env_var == "AGORA_CMS_SECRET_KEY"


def test_detect_default_admin_password():
    findings = detect_default_secrets(_settings(admin_password="agora"))
    assert len(findings) == 1
    assert findings[0].setting == "admin_password"


def test_detect_both_defaults():
    findings = detect_default_secrets(
        _settings(secret_key="change-me-in-production", admin_password="agora")
    )
    assert {f.setting for f in findings} == {"secret_key", "admin_password"}


def test_detect_tolerates_missing_attrs():
    # shared/config-only instances may not carry admin_password; function
    # must not raise on missing attributes.
    minimal = SimpleNamespace()
    assert detect_default_secrets(minimal) == []


def test_warn_on_defaults_emits_warning(caplog):
    settings = _settings(secret_key="change-me-in-production", admin_password="agora")
    with caplog.at_level(logging.WARNING, logger="agora.cms.security"):
        findings = warn_on_default_secrets(settings)
    assert len(findings) == 2
    # Each finding produces a WARNING record tagged "default-secret".
    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("AGORA_CMS_SECRET_KEY" in m for m in warning_msgs)
    assert any("AGORA_CMS_ADMIN_PASSWORD" in m for m in warning_msgs)
    assert all(m.startswith("default-secret:") for m in warning_msgs)


def test_warn_on_defaults_silent_when_clean(caplog):
    with caplog.at_level(logging.WARNING, logger="agora.cms.security"):
        findings = warn_on_default_secrets(_settings())
    assert findings == []
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


def test_warn_uses_provided_logger():
    settings = _settings(secret_key="change-me-in-production")
    captured: list[tuple[int, str]] = []

    class _Stub(logging.Logger):
        def warning(self_, msg, *args, **_kw):  # type: ignore[override]
            captured.append((logging.WARNING, msg % args if args else msg))

    stub = _Stub("stub")
    warn_on_default_secrets(settings, stub)
    assert captured, "custom logger should receive the warning"
    assert "AGORA_CMS_SECRET_KEY" in captured[0][1]
