"""Deploy-shape configuration checks.

Some settings are only consumed by specific request paths or subsystems
(image build, custom domains, transport-specific webhooks).  When such a
setting is missing the container boots fine and ``/healthz`` and the
classic ``/healthz/system`` checks all pass — the misconfig only surfaces
when a real user hits the affected code path.  That is exactly the
failure mode that took prod offline in the AGORA_CMS_BASE_URL incident.

This module centralises the list of "required-for-this-deployment-shape"
settings so:

* the lifespan startup logs a single loud ``ERROR`` line listing what's
  missing (visible in ``az containerapp logs show`` immediately after
  deploy);
* ``/healthz/system`` reports a dedicated ``config`` subsystem and
  degrades the overall ``status``, which causes the post-deploy smoke
  probe in ``publish-image.yml`` to fail and (under the upcoming
  blue/green workflow) blocks traffic from cutting over.

To register a new check:

1. Add a :class:`DeployConfigCheck` to :data:`_CHECKS` with a predicate
   that decides whether the setting is required for the current
   deployment shape, and a validator that verifies the value is sane.
2. Add a unit test in ``tests/test_deploy_config.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse


@dataclass(frozen=True)
class DeployConfigFinding:
    """A single missing-or-invalid required-for-shape setting."""

    setting: str   # pydantic field name, e.g. "base_url"
    env_var: str   # corresponding env var, e.g. "AGORA_CMS_BASE_URL"
    message: str   # operator-facing description of what's wrong


@dataclass(frozen=True)
class DeployConfigCheck:
    """Declarative entry in the deploy-shape registry."""

    setting: str
    env_var: str
    # Predicate run against ``settings`` to decide whether this setting is
    # required for the current deployment shape.  Defaults to "always
    # required".  Use this to scope checks to e.g. ``device_transport ==
    # "wps"`` if the future setting only applies in some modes.
    required_when: Callable[[object], bool]
    # Validator run against the setting's current value.  Should return a
    # human-readable error message if invalid, or ``None`` if OK.  When
    # the setting is missing entirely, the registry produces a generic
    # "is not configured" message and skips the validator.
    validate: Callable[[str], str | None]
    # Operator-facing summary (one line) used when the setting is unset.
    missing_message: str


def _validate_base_url(value: str) -> str | None:
    """Return an error string if ``value`` is not a usable BASE_URL."""
    parsed = urlparse(value.rstrip("/"))
    if parsed.scheme not in ("http", "https"):
        return (
            f"AGORA_CMS_BASE_URL={value!r} has unsupported scheme "
            f"{parsed.scheme!r}; expected http(s)"
        )
    if not parsed.netloc:
        return f"AGORA_CMS_BASE_URL={value!r} has no host component"
    if parsed.path or parsed.params or parsed.query or parsed.fragment:
        return (
            f"AGORA_CMS_BASE_URL={value!r} must be origin only "
            "(scheme + host[:port], no path/query/fragment)"
        )
    return None


_CHECKS: tuple[DeployConfigCheck, ...] = (
    DeployConfigCheck(
        setting="base_url",
        env_var="AGORA_CMS_BASE_URL",
        required_when=lambda _settings: True,
        validate=_validate_base_url,
        missing_message=(
            "AGORA_CMS_BASE_URL is not configured. Set it to the public URL "
            "of this CMS (e.g. https://agora.example.com). The image-build "
            "API and any setup-link emails depend on it."
        ),
    ),
)


def detect_missing_deploy_config(settings) -> list[DeployConfigFinding]:
    """Return a finding for every required-for-shape setting that's missing or invalid.

    Pure function — no I/O, no logging.  The registry pattern lets future
    checks be one-liners and keeps tests trivial.
    """
    findings: list[DeployConfigFinding] = []
    for check in _CHECKS:
        if not check.required_when(settings):
            continue
        value = getattr(settings, check.setting, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            findings.append(DeployConfigFinding(
                setting=check.setting,
                env_var=check.env_var,
                message=check.missing_message,
            ))
            continue
        err = check.validate(str(value))
        if err is not None:
            findings.append(DeployConfigFinding(
                setting=check.setting,
                env_var=check.env_var,
                message=err,
            ))
    return findings


def warn_on_missing_deploy_config(
    settings, logger: logging.Logger | None = None
) -> list[DeployConfigFinding]:
    """Emit a single loud ``ERROR`` log line per finding at startup.

    We log at ``ERROR`` (not WARNING) deliberately: unlike default-secret
    findings — which legitimate dev runs hit on purpose — a missing
    deploy-shape setting in any environment that bothers to wire the
    rest of the env is almost certainly a bicep/workflow regression
    that the operator wants paged on, not a "this is fine" baseline.

    We do NOT raise here: the rest of the app may still be useful (the
    UI, /healthz, jobs whose handlers don't need the missing setting).
    The post-deploy smoke probe will catch the same condition via
    /healthz/system and fail the verify step.
    """
    log = logger or logging.getLogger("agora.cms.deploy_config")
    findings = detect_missing_deploy_config(settings)
    for f in findings:
        log.error("deploy-config: %s", f.message)
    return findings
