"""Unit tests for cms.deploy_config."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from cms.deploy_config import (
    DeployConfigFinding,
    detect_missing_deploy_config,
    warn_on_missing_deploy_config,
)


def _settings(**overrides):
    base = {"base_url": "https://agora.example.com"}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_detect_no_findings_when_base_url_set():
    assert detect_missing_deploy_config(_settings()) == []


def test_detect_missing_base_url_when_none():
    findings = detect_missing_deploy_config(_settings(base_url=None))
    assert len(findings) == 1
    f = findings[0]
    assert isinstance(f, DeployConfigFinding)
    assert f.setting == "base_url"
    assert f.env_var == "AGORA_CMS_BASE_URL"
    assert "not configured" in f.message


def test_detect_missing_base_url_when_blank_string():
    findings = detect_missing_deploy_config(_settings(base_url="   "))
    assert len(findings) == 1
    assert findings[0].env_var == "AGORA_CMS_BASE_URL"


def test_detect_invalid_scheme():
    findings = detect_missing_deploy_config(_settings(base_url="ftp://x.example.com"))
    assert len(findings) == 1
    assert "scheme" in findings[0].message


def test_detect_missing_host():
    findings = detect_missing_deploy_config(_settings(base_url="https://"))
    assert len(findings) == 1
    assert "host" in findings[0].message


def test_detect_rejects_path():
    findings = detect_missing_deploy_config(_settings(base_url="https://x.example.com/cms"))
    assert len(findings) == 1
    assert "origin only" in findings[0].message


def test_detect_accepts_trailing_slash():
    # Operator-friendly: the asset/setup-link code path strips it; we
    # should not flag this as invalid.
    assert detect_missing_deploy_config(_settings(base_url="https://x.example.com/")) == []


def test_detect_accepts_port():
    assert detect_missing_deploy_config(_settings(base_url="https://x.example.com:8443")) == []


def test_detect_accepts_http_for_dev():
    assert detect_missing_deploy_config(_settings(base_url="http://localhost:8000")) == []


def test_detect_tolerates_missing_attr():
    minimal = SimpleNamespace()
    findings = detect_missing_deploy_config(minimal)
    # Treated as "not configured" rather than raising.
    assert len(findings) == 1
    assert findings[0].setting == "base_url"


def test_warn_emits_error_per_finding(caplog):
    with caplog.at_level(logging.ERROR, logger="agora.cms.deploy_config"):
        findings = warn_on_missing_deploy_config(_settings(base_url=None))
    assert len(findings) == 1
    error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("AGORA_CMS_BASE_URL" in m for m in error_msgs)
    assert all(m.startswith("deploy-config:") for m in error_msgs)


def test_warn_silent_when_clean(caplog):
    with caplog.at_level(logging.ERROR, logger="agora.cms.deploy_config"):
        findings = warn_on_missing_deploy_config(_settings())
    assert findings == []
    assert not any(r.levelno == logging.ERROR for r in caplog.records)
