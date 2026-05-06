"""Pretty-print which subsystems caused /healthz/system to degrade.

Used by the post-deploy smoke probe in
``.github/workflows/publish-image.yml`` to surface the *cause* of a
degraded status (missing env var, dead MCP, etc.) directly in the
workflow log as ``::error::`` annotations, instead of forcing the
operator to dig through container logs.

Lives as a real file (rather than a heredoc inside the workflow YAML)
because heredoc bodies inside ``run: |`` blocks must match the script's
indentation, which is fragile and easy to break.

Usage:  ``python3 scripts/explain_healthz_system.py /tmp/sys.json``
"""

from __future__ import annotations

import json
import sys


def main(path: str) -> int:
    with open(path) as f:
        data = json.load(f)

    db = data.get("db", {}) or {}
    mcp = data.get("mcp", {}) or {}
    cfg = data.get("config", {}) or {}

    if not db.get("ok", True):
        print("::error::  db: not OK")

    if mcp.get("enabled") and not mcp.get("ok", True):
        print("::error::  mcp: enabled but not reachable")

    if not cfg.get("ok", True):
        for m in cfg.get("missing", []) or []:
            env_var = m.get("env_var", "?")
            message = m.get("message", "?")
            print(f"::error::  config: {env_var} -- {message}")

    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: explain_healthz_system.py PATH_TO_SYS_JSON", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
