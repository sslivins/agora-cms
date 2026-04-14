#!/usr/bin/env python3
"""
Deploy Agora CMS infrastructure to Azure.

One-command deployment: creates the resource group, deploys all Bicep
modules, builds/pushes container images to ACR, configures MCP, and
prints connection info.

Prerequisites:
  - Python 3.9+
  - Azure CLI (az) installed and on PATH

Usage:
  python infra/deploy.py --subscription "My Azure Sub" --location westus3 --prefix agoracms

  python infra/deploy.py --subscription "My Azure Sub" --prefix agoracms \\
      --postgres-password "..." --cms-secret-key "..." --cms-admin-password "..."
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import (
    HTTPCookieProcessor,
    HTTPErrorProcessor,
    Request,
    build_opener,
    urlopen,
)

# ── Colours ──────────────────────────────────────────────────────

NO_COLOR = os.environ.get("NO_COLOR") is not None


def _c(code: str, msg: str) -> str:
    return msg if NO_COLOR else f"\033[{code}m{msg}\033[0m"


def step(msg: str) -> None:
    print(f"\n{_c('36', '▶')} {_c('36', msg)}")


def ok(msg: str) -> None:
    print(f"  {_c('32', '✓')} {_c('32', msg)}")


def warn(msg: str) -> None:
    print(f"  {_c('33', '⚠')} {_c('33', msg)}")


def fail(msg: str) -> None:
    print(f"  {_c('31', '✗')} {_c('31', msg)}")


def info(msg: str) -> None:
    print(f"  {_c('33', msg)}")


# ── Helpers ──────────────────────────────────────────────────────


def az(*args: str, capture: bool = False, check: bool = True, quiet: bool = False) -> str | None:
    """Run an az CLI command. Returns stdout when capture=True."""
    cmd = ["az", *args]
    # On Windows, az is a .cmd file — needs shell=True to resolve
    use_shell = sys.platform == "win32"
    if capture:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if quiet else subprocess.PIPE,
            text=True,
            shell=use_shell,
        )
    else:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL if quiet else None,
            stderr=subprocess.DEVNULL if quiet else None,
            shell=use_shell,
        )
    if check and result.returncode != 0:
        return None
    if capture:
        return result.stdout.strip() if result.returncode == 0 else None
    return None


def az_json(*args: str, quiet: bool = False) -> dict | list | None:
    """Run az command and parse JSON output."""
    output = az(*args, "-o", "json", capture=True, quiet=quiet)
    if output is None:
        return None
    # Filter non-JSON warning lines
    lines = output.splitlines()
    json_lines = [l for l in lines if l.strip() and not l.startswith("WARNING:")]
    try:
        return json.loads("\n".join(json_lines))
    except json.JSONDecodeError:
        return None


def http_get(url: str, timeout: int = 10) -> int | None:
    """Simple HTTP GET, returns status code or None on error."""
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            return resp.status
    except Exception:
        return None


class HttpSession:
    """HTTP session with automatic cookie handling (like requests.Session)."""

    def __init__(self) -> None:
        self._jar = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._jar))

    def post(self, url: str, data: dict | str | None = None,
             content_type: str | None = None, timeout: int = 15) -> tuple[int, str]:
        """POST with automatic cookie persistence. Returns (status, body)."""
        headers: dict[str, str] = {}

        if isinstance(data, dict):
            if content_type == "application/json":
                body = json.dumps(data).encode()
                headers["Content-Type"] = "application/json"
            else:
                body = urlencode(data).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif isinstance(data, str):
            body = data.encode()
        else:
            body = b""

        if content_type and "Content-Type" not in headers:
            headers["Content-Type"] = content_type

        req = Request(url, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode()
        except URLError as e:
            if hasattr(e, "code"):
                return e.code, e.read().decode()
            raise


# ── Main ─────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy Agora CMS infrastructure to Azure",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--subscription", required=True, help="Azure subscription name or ID")
    parser.add_argument("--location", default="westus2", help="Azure region (default: westus2)")
    parser.add_argument("--prefix", default="agora", help="Resource name prefix, 3-12 lowercase chars (default: agora)")
    parser.add_argument("--resource-group", default="", help="Resource group name (default: <prefix>-cms-rg)")
    parser.add_argument("--postgres-password", default="", help="PostgreSQL admin password")
    parser.add_argument("--cms-secret-key", default="", help="CMS secret key for JWT/session signing")
    parser.add_argument("--cms-admin-password", default="", help="CMS web admin password")
    parser.add_argument("--cms-cpu", default="1.0",
                        choices=["0.25", "0.5", "0.75", "1.0", "1.25", "1.5", "1.75", "2.0"],
                        help="CMS container CPU cores (default: 1.0, max 2.0 on Consumption tier)")
    parser.add_argument("--cms-memory", default="2Gi",
                        choices=["0.5Gi", "1Gi", "1.5Gi", "2Gi", "2.5Gi", "3Gi", "3.5Gi", "4Gi"],
                        help="CMS container memory (default: 2Gi, must be 2× CPU)")
    parser.add_argument("--skip-image-push", action="store_true", help="Skip building/pushing container images")
    args = parser.parse_args()

    # Validate prefix
    if not re.match(r"^[a-z][a-z0-9]{2,11}$", args.prefix):
        fail("Prefix must be 3-12 lowercase alphanumeric chars starting with a letter")
        return 1

    resource_group = args.resource_group or f"{args.prefix}-cms-rg"

    # ── Banner ──
    print()
    print(_c("35", "═══════════════════════════════════════════════"))
    print(_c("35", "  Agora CMS — Azure Deployment"))
    print(_c("35", "═══════════════════════════════════════════════"))

    # ── Pre-flight ──
    step("Pre-flight checks")
    if not shutil.which("az"):
        fail("Azure CLI (az) not found. Install: https://aka.ms/installazurecli")
        return 1
    ok("Azure CLI found")

    # ── Authenticate & set subscription ──
    step("Setting Azure subscription")
    account = az_json("account", "show", quiet=True)
    if not account:
        info("Launching browser login...")
        az("login", quiet=True)

    az("account", "set", "--subscription", args.subscription, quiet=True)
    sub_info = az_json("account", "show", "--query", "{name:name, id:id}")
    if not sub_info:
        fail(f"Could not set subscription '{args.subscription}'")
        info("Available subscriptions:")
        az("account", "list", "--query", "[].{Name:name, Id:id}", "-o", "table")
        return 1
    ok(f"Subscription: {sub_info['name']} ({sub_info['id']})")

    # ── Admin principal ID ──
    step("Resolving your Azure AD identity")
    admin_id = az("ad", "signed-in-user", "show", "--query", "id", "-o", "tsv", capture=True, quiet=True)
    if not admin_id:
        fail("Could not resolve signed-in user. Run 'az login' first.")
        return 1
    admin_id = admin_id.strip()
    ok(f"Admin principal: {admin_id}")

    # ── Collect secrets ──
    step("Collecting secrets")
    pg_pass = args.postgres_password or getpass.getpass("  PostgreSQL admin password: ")
    cms_key = args.cms_secret_key or getpass.getpass("  CMS secret key (for JWT/session signing): ")
    cms_pass = args.cms_admin_password or getpass.getpass("  CMS web admin password: ")

    if len(pg_pass) < 8:
        fail("PostgreSQL password must be at least 8 characters.")
        return 1
    ok("Secrets collected")

    # ── Create resource group ──
    step(f"Creating resource group: {resource_group} ({args.location})")
    az("group", "create", "--name", resource_group, "--location", args.location,
       "--tags", "project=agora-cms", "managedBy=bicep", "-o", "none", quiet=True)
    ok("Resource group ready")

    # ── Recover soft-deleted Key Vault ──
    kv_name = f"{args.prefix}-kv"
    step(f"Checking for soft-deleted Key Vault ({kv_name})")
    deleted_kv = az("keyvault", "list-deleted", "--query",
                    f"[?name=='{kv_name}'].name", "-o", "tsv", capture=True, quiet=True)
    if deleted_kv and deleted_kv.strip():
        warn(f"Found soft-deleted Key Vault '{kv_name}' — recovering")
        az("keyvault", "recover", "--name", kv_name, "-o", "none", quiet=True)
        ok("Key Vault recovered")
    else:
        ok("No soft-deleted Key Vault — clean deploy")

    # ── Resolve paths ──
    script_dir = Path(__file__).resolve().parent
    template_file = script_dir / "main.bicep"
    if not template_file.exists():
        template_file = Path.cwd() / "infra" / "main.bicep"
    if not template_file.exists():
        fail("Cannot find main.bicep. Run from the repo root or infra/ directory.")
        return 1

    repo_root = script_dir.parent
    acr_name = args.prefix.replace("-", "") + "acr"

    # ── Pre-create ACR & push images ──
    if not args.skip_image_push:
        step(f"Creating container registry ({acr_name})")
        az("acr", "create", "--name", acr_name, "--resource-group", resource_group,
           "--sku", "Basic", "--location", args.location, "--admin-enabled", "true",
           "--tags", "project=agora-cms", "managedBy=bicep", "-o", "none", quiet=True)
        ok("ACR ready")

        step(f"Building container images in ACR ({acr_name})")

        info("Building CMS image...")
        az("acr", "build", "--registry", acr_name, "--image", "agora-cms:latest",
           "--file", str(repo_root / "Dockerfile"), str(repo_root), "--no-logs", quiet=True)
        ok("CMS image built & pushed")

        info("Building MCP image...")
        az("acr", "build", "--registry", acr_name, "--image", "agora-cms-mcp:latest",
           "--file", str(repo_root / "mcp" / "Dockerfile"), str(repo_root / "mcp"), "--no-logs", quiet=True)
        ok("MCP image built & pushed")
    else:
        warn("Skipping image build (--skip-image-push)")

    # ── Deploy Bicep ──
    step("Deploying infrastructure (this takes 5-10 minutes)...")
    deploy_output = az(
        "deployment", "group", "create",
        "--resource-group", resource_group,
        "--template-file", str(template_file),
        "--parameters",
        f"prefix={args.prefix}",
        f"location={args.location}",
        f"postgresAdminPassword={pg_pass}",
        f"cmsSecretKey={cms_key}",
        f"cmsAdminPassword={cms_pass}",
        f"cmsCpu={args.cms_cpu}",
        f"cmsMemory={args.cms_memory}",
        f"adminPrincipalId={admin_id}",
        "--query", "properties.outputs",
        "-o", "json",
        capture=True, quiet=True,
    )
    if not deploy_output:
        fail("Bicep deployment failed. Run with verbose output:")
        print(f"  az deployment group create --resource-group {resource_group} "
              f"--template-file {template_file} ...")
        return 1

    outputs = json.loads(deploy_output)
    cms_url = outputs["cmsUrl"]["value"]
    mcp_url = outputs["mcpUrl"]["value"]
    acr_login = outputs["acrLoginServer"]["value"]
    pg_fqdn = outputs["postgresServerFqdn"]["value"]
    kv_uri = outputs["keyVaultUri"]["value"]
    storage_name = outputs["storageAccountName"]["value"]

    ok("Infrastructure deployed")

    # ── Post-deploy: configure MCP ──
    step("Configuring MCP server")
    cms_app = f"{args.prefix}-cms"
    mcp_app = f"{args.prefix}-mcp"
    mcp_sse_key = ""

    info("Waiting for CMS to start...")
    cms_ready = False
    for i in range(1, 31):
        if http_get(f"https://{cms_url}/login") == 200:
            cms_ready = True
            break
        if i == 30:
            warn("CMS not responding after 30 attempts — MCP setup skipped")
            info("You can configure it manually later.")
        time.sleep(10)

    if cms_ready:
        try:
            session = HttpSession()

            # Login (follows 303 redirect, cookies auto-captured)
            session.post(f"https://{cms_url}/login",
                         data={"username": "admin", "password": cms_pass})

            # Enable MCP (auto-provisions service key)
            session.post(
                f"https://{cms_url}/api/mcp/toggle",
                data={"enabled": True},
                content_type="application/json",
            )
            ok("MCP server enabled (service key auto-provisioned)")

            # Generate MCP SSE auth key for admin user
            status, body = session.post(
                f"https://{cms_url}/api/mcp/generate-key",
                content_type="application/json",
            )
            mcp_sse_key = json.loads(body)["key"]
            ok("MCP SSE auth key generated")

        except Exception as e:
            warn(f"Failed to configure MCP: {e}")
            info("You can configure it manually via the CMS settings page.")
            mcp_sse_key = ""

    # ── Summary ──
    print()
    print(_c("32", "═══════════════════════════════════════════════"))
    print(_c("32", "  Deployment Complete!"))
    print(_c("32", "═══════════════════════════════════════════════"))
    print()
    print(f"  CMS URL:          https://{cms_url}")
    print(f"  MCP URL:          https://{mcp_url}")
    print(f"  ACR:              {acr_login}")
    print(f"  PostgreSQL:       {pg_fqdn}")
    print(f"  Key Vault:        {kv_uri}")
    print(f"  Storage Account:  {storage_name}")
    print(f"  Resource Group:   {resource_group}")
    if mcp_sse_key:
        print()
        print(f"  MCP SSE Auth:     Bearer {mcp_sse_key}")
        print(f"  MCP SSE URL:      https://{mcp_url}/sse")
    print()

    # ── Save outputs ──
    output_file = script_dir / "deployment-outputs.json"
    output_obj = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "subscription": sub_info["name"],
        "resourceGroup": resource_group,
        "location": args.location,
        "prefix": args.prefix,
        "cmsUrl": f"https://{cms_url}",
        "mcpUrl": f"https://{mcp_url}",
        "mcpSseUrl": f"https://{mcp_url}/sse",
        "mcpSseKey": mcp_sse_key,
        "acrLoginServer": acr_login,
        "postgresServerFqdn": pg_fqdn,
        "keyVaultUri": kv_uri,
        "storageAccountName": storage_name,
    }
    output_file.write_text(json.dumps(output_obj, indent=2), encoding="utf-8")
    ok(f"Outputs saved to {output_file}")

    # ── Next steps ──
    print()
    print(_c("33", "  Next steps:"))
    print(f"    1. Open CMS: https://{cms_url}")
    print(f"    2. Login with admin / <your password>")
    if mcp_sse_key:
        print(f"    3. Configure MCP in Copilot CLI using the SSE URL and key above")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
