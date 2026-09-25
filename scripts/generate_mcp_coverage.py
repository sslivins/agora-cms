#!/usr/bin/env python
"""Generate docs/mcp-coverage.md — the MCP tool coverage manifest.

Why this exists
---------------
The MCP server (``mcp/server.py``) mirrors a subset of the CMS HTTP API as
LLM-callable tools. Nothing previously connected the two, so a feature could
ship a full set of API routes and a UI while the MCP server silently gained
nothing — which is exactly what happened with voice announcements.

This script enumerates every ``/api/**`` route and records which MCP tool (if
any) exposes it. The generated file is committed, and ``mcp-check`` in CI
regenerates it and fails on any diff — the same contract ``openapi-check``
uses for ``docs/openapi.yaml``.

The manifest is deliberately **descriptive, not prescriptive**. It does not
assert that every route ought to have a tool; plenty legitimately should not.
Its job is to make a new uncovered route show up as a visible line in the pull
request diff, so exposing it over MCP becomes a decision someone makes rather
than one that defaults to "no" by oversight.

Run ``python scripts/generate_mcp_coverage.py`` and commit the result.

Route source
------------
Routes come from the committed ``docs/openapi.yaml`` rather than by importing
the FastAPI app. ``openapi-check`` already guarantees that spec matches the
code, so the two checks compose: openapi-check pins spec-to-code, and this one
pins spec-to-tools. It also keeps this script free of the full application
import (and its dependency tree), which is what broke the first version of
this check in CI.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OUTPUT = REPO_ROOT / "docs" / "mcp-coverage.md"
SPEC = REPO_ROOT / "docs" / "openapi.yaml"
CLIENT_PY = REPO_ROOT / "mcp" / "cms_client.py"
SERVER_PY = REPO_ROOT / "mcp" / "server.py"

HTTP_HELPERS = {"_get": "GET", "_post": "POST", "_put": "PUT", "_delete": "DELETE", "_patch": "PATCH"}

# Guard against silently publishing an empty manifest if the spec ever fails
# to parse or arrives truncated. The real number is in the 160s; anything
# under this means something is wrong with the inputs, not with the API.
MIN_PLAUSIBLE_ROUTES = 100


def _path_from_arg(node: ast.expr) -> str | None:
    """Render a literal or f-string path argument as a route template.

    ``f"/api/assets/{asset_id}"`` becomes ``/api/assets/{}`` so it can be
    compared against the FastAPI route ``/api/assets/{asset_id}``.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        return "".join(parts)
    return None


def client_method_routes() -> dict[str, tuple[str, str]]:
    """Map each CMSClient method name to the (verb, path) it calls."""
    tree = ast.parse(CLIENT_PY.read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if node.name.startswith("_"):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            fn = inner.func
            if not (isinstance(fn, ast.Attribute) and fn.attr in HTTP_HELPERS):
                continue
            if not inner.args:
                continue
            path = _path_from_arg(inner.args[0])
            if path is not None:
                out[node.name] = (HTTP_HELPERS[fn.attr], path)
                break
    return out


def tool_to_client_methods() -> dict[str, list[str]]:
    """Map each @mcp.tool() function to the CMSClient methods it invokes.

    Tools reach the API exclusively through ``_call_api("<client method>", …)``,
    so the first argument of that call is the link between the two files.
    """
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        is_tool = any(
            (isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "tool")
            or getattr(d, "attr", None) == "tool"
            for d in node.decorator_list
        )
        if not is_tool:
            continue
        methods: list[str] = []
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and getattr(inner.func, "id", None) == "_call_api"
                and inner.args
                and isinstance(inner.args[0], ast.Constant)
            ):
                name = inner.args[0].value
                if name not in methods:
                    methods.append(name)
        out[node.name] = methods
    return out


def api_routes() -> list[tuple[str, str]]:
    """Every /api/** operation documented in docs/openapi.yaml."""
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    seen: set[tuple[str, str]] = set()
    for path, item in (spec.get("paths") or {}).items():
        if not path.startswith("/api/") or not isinstance(item, dict):
            continue
        for method in item:
            if method.lower() in ("get", "post", "put", "delete", "patch"):
                seen.add((method.upper(), path))
    return sorted(seen, key=lambda r: (r[1], r[0]))


def _template(path: str) -> str:
    """Collapse named path params so both sides compare equal."""
    out, depth = [], 0
    for ch in path:
        if ch == "{":
            depth += 1
            if depth == 1:
                out.append("{}")
        elif ch == "}":
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def main() -> None:
    client_routes = client_method_routes()
    tool_methods = tool_to_client_methods()

    # (verb, templated path) -> tool names covering it
    covered: dict[tuple[str, str], list[str]] = {}
    for tool, methods in tool_methods.items():
        for method_name in methods:
            route = client_routes.get(method_name)
            if not route:
                continue
            key = (route[0], _template(route[1]))
            covered.setdefault(key, []).append(tool)

    routes = api_routes()
    if len(routes) < MIN_PLAUSIBLE_ROUTES:
        raise SystemExit(
            f"Refusing to write manifest: only {len(routes)} /api routes found in "
            f"{SPEC.relative_to(REPO_ROOT)} (expected at least {MIN_PLAUSIBLE_ROUTES}). "
            "The spec is probably missing or truncated - regenerate it with "
            "'python scripts/generate_openapi.py' first."
        )
    rows = []
    covered_count = 0
    for verb, path in routes:
        tools = sorted(covered.get((verb, _template(path)), []))
        if tools:
            covered_count += 1
        rows.append((verb, path, ", ".join(f"`{t}`" for t in tools) if tools else "—"))

    total = len(routes)
    lines = [
        "# MCP tool coverage",
        "",
        "<!-- GENERATED FILE — DO NOT EDIT BY HAND.",
        "     Run `python scripts/generate_mcp_coverage.py` and commit the result.",
        "     CI job `mcp-check` fails if this file is out of sync. -->",
        "",
        "Every `/api/**` route in the CMS, and the MCP tool that exposes it to the",
        "Assistant (`mcp/server.py`), if any. Routes are taken from",
        "`docs/openapi.yaml`, which `openapi-check` keeps in sync with the code.",
        "",
        "**A `—` is not a bug.** Many routes are UI plumbing, device-facing, or",
        "otherwise a poor fit for an LLM tool. The point of this file is that when a",
        "new feature adds routes, they appear here as new `—` lines in the pull",
        "request diff — so *whether* to expose them over MCP becomes an explicit",
        "decision instead of an oversight.",
        "",
        f"Coverage: **{covered_count} of {total}** `/api` routes exposed as MCP tools.",
        "",
        "| Method | Path | MCP tool |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {verb} | `{path}` | {tools} |" for verb, path, tools in rows]
    lines.append("")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)} ({covered_count}/{total} routes covered)")


if __name__ == "__main__":
    main()
