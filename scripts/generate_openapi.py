#!/usr/bin/env python3
"""Generate ``docs/openapi.yaml`` from the live FastAPI app.

``docs/openapi.yaml`` is the canonical, machine-generated OpenAPI 3 spec for
the CMS REST API. It is **not** hand-edited; CI verifies it stays in sync with
the route definitions in ``cms/`` (see ``.github/workflows/tests.yml`` ->
``openapi-check`` job). To pick up a route or schema change, regenerate with::

    python scripts/generate_openapi.py

then commit the updated ``docs/openapi.yaml`` with your change.

Routes meant only for browser navigation live on ``cms.ui.router``, which is
constructed with ``include_in_schema=False`` and so does not appear here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "docs" / "openapi.yaml"

# Make ``cms`` importable when running this script from anywhere.
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from cms.main import app  # noqa: E402


# Things FastAPI cannot infer from the route table on its own.

SERVERS: list[dict[str, str]] = [
    {"url": "http://localhost:8080", "description": "Local Docker Compose"},
]

# The CMS authenticates browser sessions with a cookie and device/automation
# clients with a static API key header. FastAPI does not auto-emit these
# because the ``require_auth`` dependency reads ``request.cookies`` /
# ``request.headers`` directly rather than going through a built-in security
# scheme class. Declare them explicitly here so the spec is usable.
SECURITY_SCHEMES: dict[str, dict[str, str]] = {
    "sessionCookie": {
        "type": "apiKey",
        "in": "cookie",
        "name": "session",
        "description": "Browser session cookie issued by POST /login.",
    },
    "apiKey": {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": (
            "Static API key issued via /api/keys; used for device-originated "
            "and automation callers."
        ),
    },
}

TAG_DESCRIPTIONS: dict[str, str] = {
    "dashboard": "Dashboard overview data.",
    "devices": "Device management (CMS-originated).",
    "devices (device-originated)": "Endpoints called by the device firmware itself.",
    "bootstrap": "First-contact registration / adoption endpoints used during device onboarding.",
    "groups": "Device groups.",
    "assets": "Asset library, upload, and transcoding.",
    "schedules": "Content scheduling.",
    "profiles": "Transcoding profiles applied to assets.",
    "users": "User accounts.",
    "roles": "Roles and permissions.",
    "api-keys": "Static API keys for non-interactive callers.",
    "audit": "Audit log.",
    "device-events": "Device-emitted event stream.",
    "notifications": "In-CMS notifications.",
    "notification-preferences": "Per-user notification preferences.",
    "logs": "Log retrieval and upload.",
    "imager": "OS-image build pipeline.",
    "stream-probe": "Probe upstream stream URLs for technical metadata.",
    "issues": "User-reported issues.",
    "mcp": "Model Context Protocol integration.",
    "system": "Health checks and system endpoints.",
    "wps-webhook": "Inbound WebPubSub event grid webhooks.",
}


def _str_presenter(dumper: yaml.Dumper, data: str):
    """Render multi-line strings as literal blocks for readable diffs."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


def _augment(spec: dict[str, Any]) -> dict[str, Any]:
    spec["servers"] = SERVERS

    components = spec.setdefault("components", {})
    schemes = components.setdefault("securitySchemes", {})
    for name, defn in SECURITY_SCHEMES.items():
        schemes.setdefault(name, defn)

    # Default to session cookie globally; routes can override with their own
    # ``security`` block (e.g. device-originated routes that take an API key).
    spec.setdefault("security", [{"sessionCookie": []}])

    # Build the tag list: known tags get their canonical description, unknown
    # tags discovered from routes are still listed so the spec is complete.
    existing = {t.get("name"): dict(t) for t in spec.get("tags", [])}
    seen = set(existing)
    for path_item in spec.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for op in path_item.values():
            if not isinstance(op, dict):
                continue
            for t in op.get("tags", []) or []:
                if t not in seen:
                    existing[t] = {"name": t}
                    seen.add(t)
    for name in existing:
        if name in TAG_DESCRIPTIONS:
            existing[name]["description"] = TAG_DESCRIPTIONS[name]
    spec["tags"] = sorted(existing.values(), key=lambda t: t["name"])

    return spec


def main() -> int:
    spec = _augment(app.openapi())

    yaml.SafeDumper.add_representer(str, _str_presenter)

    text = yaml.safe_dump(
        spec,
        sort_keys=False,
        width=120,
        default_flow_style=False,
        allow_unicode=True,
        indent=2,
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text, encoding="utf-8", newline="\n")

    paths = len(spec.get("paths", {}))
    schemas = len(spec.get("components", {}).get("schemas", {}))
    print(
        f"Wrote {OUTPUT.relative_to(REPO_ROOT)} "
        f"({paths} paths, {schemas} schemas, {len(text)} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
