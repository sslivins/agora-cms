"""Security posture checks run at app startup (#308).

Keeps security-related runtime checks out of main.py and auth.py so they're
easy to find and unit-test in isolation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass


# Values that ship as defaults in cms/config.py and shared/config.py.
# If the running instance still carries these, the operator almost
# certainly forgot to set the corresponding AGORA_CMS_* env var.
_DEFAULT_SECRET_KEY = "change-me-in-production"
_DEFAULT_ADMIN_PASSWORD = "agora"


@dataclass(frozen=True)
class SecretFinding:
    """A single default-credential detection."""

    setting: str   # pydantic field name (e.g. "secret_key")
    env_var: str   # corresponding env var name (e.g. "AGORA_CMS_SECRET_KEY")
    message: str


def detect_default_secrets(settings) -> list[SecretFinding]:
    """Return a list of findings for settings still at their shipped defaults.

    Pure function — no I/O, no logging. Makes the detection logic trivially
    unit-testable and reusable by CI tooling (e.g. a future pre-deploy
    validation script).
    """
    findings: list[SecretFinding] = []
    if getattr(settings, "secret_key", None) == _DEFAULT_SECRET_KEY:
        findings.append(SecretFinding(
            setting="secret_key",
            env_var="AGORA_CMS_SECRET_KEY",
            message=(
                "AGORA_CMS_SECRET_KEY is at its shipped default "
                f"({_DEFAULT_SECRET_KEY!r}). Session cookies and CSRF tokens "
                "signed with this key are trivially forgeable. Set "
                "AGORA_CMS_SECRET_KEY to a 32+ char random string before "
                "exposing this instance to untrusted networks."
            ),
        ))
    if getattr(settings, "admin_password", None) == _DEFAULT_ADMIN_PASSWORD:
        findings.append(SecretFinding(
            setting="admin_password",
            env_var="AGORA_CMS_ADMIN_PASSWORD",
            message=(
                "AGORA_CMS_ADMIN_PASSWORD is at its shipped default "
                f"({_DEFAULT_ADMIN_PASSWORD!r}). The admin account is "
                "trivially takeable. Set AGORA_CMS_ADMIN_PASSWORD before "
                "first boot, or change the admin password via the UI."
            ),
        ))
    return findings


def warn_on_default_secrets(settings, logger: logging.Logger | None = None) -> list[SecretFinding]:
    """Emit a WARNING log line for each default-credential finding.

    Returns the findings so callers (and tests) can inspect the result. We
    log a warning rather than raising: many legitimate dev / CI runs
    genuinely want the defaults, and a startup abort would break those
    workflows. Production operators should grep for ``default-secret`` in
    their logs and rotate before opening the instance to the network.
    """
    log = logger or logging.getLogger("agora.cms.security")
    findings = detect_default_secrets(settings)
    for f in findings:
        log.warning("default-secret: %s", f.message)
    return findings
