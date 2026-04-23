"""Agora CMS MCP Server — exposes CMS operations as MCP tools over SSE."""

import asyncio
import contextvars
import json
import logging
import os
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from cms_client import CMSClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CMS_BASE_URL = os.environ.get("CMS_BASE_URL", "http://cms:8080")
SERVICE_KEY_PATH = os.environ.get("SERVICE_KEY_PATH", "/shared/mcp-service.key")
AZURE_KEYVAULT_URI = os.environ.get("AZURE_KEYVAULT_URI", "")

# Module-level service key — loaded at startup, reloaded on demand via /reload-key
_service_key: str = ""
_service_key_lock = asyncio.Lock()

mcp = FastMCP(
    "Agora CMS",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)

# Per-request context — set by BearerAuthMiddleware, read by tools
_ctx_api_key: contextvars.ContextVar[str] = contextvars.ContextVar("api_key")
_ctx_permissions: contextvars.ContextVar[list[str]] = contextvars.ContextVar("permissions", default=[])
_ctx_user_name: contextvars.ContextVar[str] = contextvars.ContextVar("user_name", default="unknown")

# Tool → required permission mapping
TOOL_PERMISSIONS: dict[str, str | None] = {
    "list_devices": "devices:read",
    "get_device": "devices:read",
    "adopt_device": "devices:write",
    "update_device": "devices:write",
    "reboot_device": "devices:reboot",
    "delete_device": "devices:delete",
    "list_groups": "groups:read",
    "create_group": "groups:write",
    "update_group": "groups:write",
    "delete_group": "groups:write",
    "list_assets": "assets:read",
    "get_asset": "assets:read",
    "delete_asset": "assets:write",
    "create_webpage_asset": "assets:write",
    "list_schedules": "schedules:read",
    "get_schedule": "schedules:read",
    "create_schedule": "schedules:write",
    "update_schedule": "schedules:write",
    "delete_schedule": "schedules:write",
    "play_now": "schedules:write",
    "end_schedule_now": "schedules:write",
    "list_profiles": "profiles:read",
    "get_device_logs": "logs:read",
    "get_server_time": None,  # any authenticated user
    "get_dashboard": None,    # any authenticated user
    "list_audit_events": "audit:read",
}


def _get_client() -> CMSClient:
    """Return a CMSClient using the service key and current user's identity."""
    return CMSClient(
        base_url=CMS_BASE_URL,
        api_key=_service_key,
        on_behalf_of=_ctx_user_name.get(),
    )


async def _call_api(method_name: str, *args, **kwargs):
    """Call a CMSClient method with automatic key reload on 401.

    If the CMS returns 401 (stale service key), reloads the key from
    Key Vault/file and retries once with a fresh client.
    """
    client = _get_client()
    try:
        return await getattr(client, method_name)(*args, **kwargs)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401:
            raise
        logger.warning("CMS returned 401 — reloading service key and retrying")
        await _reload_service_key()
        client = _get_client()
        return await getattr(client, method_name)(*args, **kwargs)


def _check_permission(tool_name: str) -> str | None:
    """Check if the current user has permission for a tool.

    Returns None if allowed, or an error message if denied.
    """
    required = TOOL_PERMISSIONS.get(tool_name)
    if required is None:
        return None
    perms = _ctx_permissions.get()
    if required not in perms:
        user = _ctx_user_name.get()
        return (
            f"Permission denied: '{tool_name}' requires the '{required}' permission. "
            f"Your account ({user}) does not have this permission."
        )
    return None


def _json_result(data) -> str:
    """Format result as indented JSON for readability."""
    return json.dumps(data, indent=2, default=str)


# ── Devices ──


@mcp.tool()
async def list_devices() -> str:
    """List all registered Agora devices with their status, group, and connection state."""
    if err := _check_permission("list_devices"):
        return err
    devices = await _call_api("list_devices")
    return _json_result(devices)


@mcp.tool()
async def get_device(device_id: str) -> str:
    """Get detailed information about a specific device.

    Args:
        device_id: The device ID (Pi serial number or UUID).
    """
    if err := _check_permission("get_device"):
        return err
    device = await _call_api("get_device", device_id)
    return _json_result(device)


@mcp.tool()
async def adopt_device(device_id: str) -> str:
    """Adopt a pending device so it can receive schedules and content.

    Args:
        device_id: The device ID to adopt.
    """
    if err := _check_permission("adopt_device"):
        return err
    result = await _call_api("adopt_device", device_id)
    return _json_result(result)


@mcp.tool()
async def update_device(
    device_id: str,
    name: str | None = None,
    group_id: str | None = None,
    default_asset_id: str | None = None,
) -> str:
    """Update a device's name, group assignment, or default asset (splash screen).

    Args:
        device_id: The device ID to update.
        name: New display name for the device.
        group_id: UUID of the group to assign the device to, or null to remove from group.
        default_asset_id: UUID of the asset to use as the device's default splash screen,
            or null to clear (falls back to group default, then system splash).
    """
    if err := _check_permission("update_device"):
        return err
    fields = {}
    if name is not None:
        fields["name"] = name
    if group_id is not None:
        fields["group_id"] = group_id
    if default_asset_id is not None:
        fields["default_asset_id"] = default_asset_id if default_asset_id != "null" else None
    result = await _call_api("update_device", device_id, fields)
    return _json_result(result)


@mcp.tool()
async def reboot_device(device_id: str) -> str:
    """Send a reboot command to a device.

    Args:
        device_id: The device ID to reboot.
    """
    if err := _check_permission("reboot_device"):
        return err
    await _call_api("reboot_device", device_id)
    return f"Reboot command sent to device {device_id}"


@mcp.tool()
async def delete_device(device_id: str) -> str:
    """Remove a device from the CMS. The device will need to be re-adopted.

    Args:
        device_id: The device ID to delete.
    """
    if err := _check_permission("delete_device"):
        return err
    await _call_api("delete_device", device_id)
    return f"Device {device_id} deleted"


# ── Groups ──


@mcp.tool()
async def list_groups() -> str:
    """List all device groups."""
    if err := _check_permission("list_groups"):
        return err
    groups = await _call_api("list_groups")
    return _json_result(groups)


@mcp.tool()
async def create_group(
    name: str,
    description: str = "",
    default_asset_id: str | None = None,
) -> str:
    """Create a new device group for bulk scheduling.

    Args:
        name: Name of the group.
        description: Optional description.
        default_asset_id: UUID of the asset to use as the default splash screen for all
            devices in this group (unless overridden at the device level).
    """
    if err := _check_permission("create_group"):
        return err
    result = await _call_api("create_group", name, description, default_asset_id=default_asset_id)
    return _json_result(result)


@mcp.tool()
async def update_group(
    group_id: str,
    name: str | None = None,
    description: str | None = None,
    default_asset_id: str | None = None,
) -> str:
    """Update a device group's name, description, or default asset.

    Args:
        group_id: UUID of the group to update.
        name: New name for the group.
        description: New description.
        default_asset_id: UUID of the asset to use as the default splash screen for all
            devices in this group, or null to clear. Device-level defaults take precedence.
    """
    if err := _check_permission("update_group"):
        return err
    fields = {}
    if name is not None:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if default_asset_id is not None:
        fields["default_asset_id"] = default_asset_id if default_asset_id != "null" else None
    result = await _call_api("update_group", group_id, fields)
    return _json_result(result)


@mcp.tool()
async def delete_group(group_id: str) -> str:
    """Delete a device group. Devices in the group will be ungrouped.

    Args:
        group_id: UUID of the group to delete.
    """
    if err := _check_permission("delete_group"):
        return err
    await _call_api("delete_group", group_id)
    return f"Group {group_id} deleted"


# ── Assets ──


@mcp.tool()
async def list_assets() -> str:
    """List all uploaded assets (videos, images, and webpages) in the CMS library."""
    if err := _check_permission("list_assets"):
        return err
    assets = await _call_api("list_assets")
    return _json_result(assets)


@mcp.tool()
async def get_asset(asset_id: str) -> str:
    """Get detailed information about a specific asset.

    Args:
        asset_id: UUID of the asset.
    """
    if err := _check_permission("get_asset"):
        return err
    asset = await _call_api("get_asset", asset_id)
    return _json_result(asset)


@mcp.tool()
async def delete_asset(asset_id: str) -> str:
    """Delete an asset from the CMS library. Removes the file and all variants.

    Args:
        asset_id: UUID of the asset to delete.
    """
    if err := _check_permission("delete_asset"):
        return err
    await _call_api("delete_asset", asset_id)
    return f"Asset {asset_id} deleted"


@mcp.tool()
async def create_webpage_asset(
    url: str,
    name: str | None = None,
    group_id: str | None = None,
) -> str:
    """Create a webpage asset from a URL. No file upload needed.

    Webpage assets render a URL on-screen using Chromium in kiosk mode.
    Only supported on Raspberry Pi 5 and Compute Module 5 devices.

    Args:
        url: The webpage URL to display (must start with http:// or https://).
        name: Optional display name. If omitted, derived from the URL hostname.
        group_id: Optional UUID of the group to assign the asset to.
    """
    if err := _check_permission("create_webpage_asset"):
        return err
    data: dict = {"url": url}
    if name:
        data["name"] = name
    if group_id:
        data["group_id"] = group_id
    result = await _call_api("create_webpage_asset", data)
    return _json_result(result)


# ── Schedules ──


@mcp.tool()
async def list_schedules() -> str:
    """List all schedules with their target devices/groups, assets, and time windows."""
    if err := _check_permission("list_schedules"):
        return err
    schedules = await _call_api("list_schedules")
    return _json_result(schedules)


@mcp.tool()
async def get_schedule(schedule_id: str) -> str:
    """Get detailed information about a specific schedule.

    Args:
        schedule_id: UUID of the schedule.
    """
    if err := _check_permission("get_schedule"):
        return err
    schedule = await _call_api("get_schedule", schedule_id)
    return _json_result(schedule)


@mcp.tool()
async def create_schedule(
    name: str,
    asset_id: str,
    start_time: str,
    group_id: str,
    end_time: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    days_of_week: list[int] | None = None,
    priority: int = 0,
    enabled: bool = True,
    loop_count: int | None = None,
) -> str:
    """Create a new playback schedule.

    Assigns an asset to play on a device group during a time window.

    IMPORTANT — loop_count vs end_time:
    When loop_count is set, end_time is IGNORED and overridden to
    start_time + (loop_count × asset_duration). This creates a narrow
    playback window that may already have passed. For ad-hoc playback,
    use the play_now tool instead.

    IMPORTANT — timezone:
    All times (start_time, end_time) are interpreted in the CMS server's
    configured timezone, NOT UTC. Use get_server_time to check the
    server's timezone and current local time before creating schedules.

    Args:
        name: Schedule display name.
        asset_id: UUID of the asset to play.
        start_time: Start time in HH:MM:SS format (e.g. "08:00:00"), interpreted in the CMS server's timezone.
        group_id: Target group UUID.
        end_time: End time in HH:MM:SS format. IGNORED when loop_count is set (auto-computed to start_time + loop_count × asset_duration).
        start_date: Optional start date in ISO format (e.g. "2026-04-10T00:00:00Z").
        end_date: Optional end date in ISO format.
        days_of_week: Optional list of ISO weekday numbers (1=Monday, 7=Sunday).
        priority: Schedule priority (higher wins when overlapping). Default 0.
        enabled: Whether the schedule is active. Default true.
        loop_count: Number of times to loop the asset. When set, end_time is IGNORED and auto-computed. Omit or set null for infinite looping within the time window.
    """
    if err := _check_permission("create_schedule"):
        return err
    data = {
        "name": name,
        "asset_id": asset_id,
        "start_time": start_time,
        "group_id": group_id,
        "priority": priority,
        "enabled": enabled,
    }
    if end_time:
        data["end_time"] = end_time
    if start_date:
        data["start_date"] = start_date
    if end_date:
        data["end_date"] = end_date
    if days_of_week:
        data["days_of_week"] = days_of_week
    if loop_count is not None:
        data["loop_count"] = loop_count

    result = await _call_api("create_schedule", data)
    return _json_result(result)


@mcp.tool()
async def update_schedule(
    schedule_id: str,
    name: str | None = None,
    asset_id: str | None = None,
    group_id: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    days_of_week: list[int] | None = None,
    priority: int | None = None,
    enabled: bool | None = None,
    loop_count: int | None = None,
) -> str:
    """Update an existing schedule. Only provided fields are changed.

    Args:
        schedule_id: UUID of the schedule to update.
        name: New display name.
        asset_id: New asset UUID.
        group_id: New target group UUID.
        start_time: New start time (HH:MM:SS).
        end_time: New end time (HH:MM:SS).
        start_date: New start date (ISO format).
        end_date: New end date (ISO format).
        days_of_week: New list of ISO weekday numbers.
        priority: New priority value.
        enabled: Enable or disable the schedule.
        loop_count: New loop count (null = infinite).
    """
    if err := _check_permission("update_schedule"):
        return err
    fields = {}
    for key in ("name", "asset_id", "group_id", "start_time",
                "end_time", "start_date", "end_date", "days_of_week",
                "priority", "enabled", "loop_count"):
        val = locals()[key]
        if val is not None:
            fields[key] = val
    result = await _call_api("update_schedule", schedule_id, fields)
    return _json_result(result)


@mcp.tool()
async def delete_schedule(schedule_id: str) -> str:
    """Delete a schedule.

    Args:
        schedule_id: UUID of the schedule to delete.
    """
    if err := _check_permission("delete_schedule"):
        return err
    await _call_api("delete_schedule", schedule_id)
    return f"Schedule {schedule_id} deleted"


@mcp.tool()
async def play_now(
    group_id: str,
    asset_id: str,
    name: str | None = None,
) -> str:
    """Immediately play an asset on a device group. Creates a high-priority schedule
    for today with an all-day time window so playback starts right away.

    When done, call end_schedule_now with the returned schedule ID to stop
    playback, or delete_schedule to remove it entirely.

    Args:
        group_id: The target group UUID to play on.
        asset_id: UUID of the asset to play.
        name: Optional schedule name. Defaults to "<asset_filename> — Play Now".
    """
    if err := _check_permission("play_now"):
        return err
    if not name:
        try:
            asset = await _call_api("get_asset", asset_id)
            asset_name = asset.get("original_filename") or asset.get("filename") or asset_id
            name = f"{asset_name} \u2014 Play Now"
        except Exception:
            name = f"Play Now \u2014 {asset_id[:8]}"

    server_time = await _call_api("get_server_time")
    today = server_time["local"][:10]
    data = {
        "name": name,
        "asset_id": asset_id,
        "group_id": group_id,
        "start_time": "00:00:00",
        "end_time": "23:59:00",
        "start_date": f"{today}T00:00:00Z",
        "end_date": f"{today}T23:59:59Z",
        "priority": 10,
        "enabled": True,
    }
    result = await _call_api("create_schedule", data)
    schedule_id = result.get("id", "unknown")
    return _json_result({
        "schedule": result,
        "hint": f"Playback started. To stop, call end_schedule_now('{schedule_id}') or delete_schedule('{schedule_id}').",
    })


@mcp.tool()
async def end_schedule_now(schedule_id: str) -> str:
    """End a currently-playing schedule immediately. It will resume at its next scheduled time.

    Args:
        schedule_id: UUID of the schedule to end.
    """
    if err := _check_permission("end_schedule_now"):
        return err
    await _call_api("end_schedule_now", schedule_id)
    return f"Schedule {schedule_id} ended for current occurrence"


# ── Profiles ──


@mcp.tool()
async def list_profiles() -> str:
    """List all transcode profiles (codec settings used when preparing video assets for devices)."""
    if err := _check_permission("list_profiles"):
        return err
    profiles = await _call_api("list_profiles")
    return _json_result(profiles)


# ── Logs ──


@mcp.tool()
async def get_device_logs(
    device_id: str,
    services: list[str] | None = None,
    since: str = "24h",
) -> str:
    """Request logs from a connected device and return the captured output.

    Creates an async log request on the CMS, waits for the device to
    reply (up to ~60s), and returns the captured journalctl output as
    ``{service_name: log_text}``.  If the device doesn't reply in time,
    returns the request_id and status so the caller can retrieve it
    later via the CMS UI or API.

    Args:
        device_id: ID of the device to get logs from.
        services: Optional list of systemd service names to filter (e.g. ["agora-player", "agora-api"]).
                  If omitted, returns logs from all agora services.
        since: Time range for logs (e.g. "1h", "24h", "7d"). Default "24h".
    """
    if err := _check_permission("get_device_logs"):
        return err
    result = await _call_api("request_device_logs", device_id, services=services, since=since)
    return _json_result(result)


# ── Server time ──


@mcp.tool()
async def get_server_time() -> str:
    """Get the CMS server's configured timezone and current local time.

    Use this before creating schedules to understand what timezone
    start_time and end_time will be interpreted in.
    """
    data = await _call_api("get_server_time")
    return _json_result(data)


# ── Dashboard ──


@mcp.tool()
async def get_dashboard() -> str:
    """Get the current dashboard state: what's playing now, upcoming schedules, device states, and alerts."""
    dashboard = await _call_api("get_dashboard")
    return _json_result(dashboard)


# ── Audit log ──


@mcp.tool()
async def list_audit_events(
    limit: int = 50,
    offset: int = 0,
    action: str | None = None,
    resource_type: str | None = None,
    user_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    q: str | None = None,
) -> str:
    """Query the CMS audit log. Returns the most recent events first.

    Use this to answer forensic questions like 'who deleted asset X',
    'what changed in the last hour', or 'what has user Y done today'.

    Args:
        limit: Max events to return (1-500, default 50).
        offset: Pagination offset (default 0).
        action: Filter by action string (e.g. 'asset.delete', 'schedule.create').
        resource_type: Filter by resource type ('asset', 'schedule', 'device',
            'user', 'group', 'profile').
        resource_id: (not supported server-side yet) — use q instead.
        user_id: Filter by acting user UUID.
        since: ISO-8601 timestamp; only events at or after this time.
        until: ISO-8601 timestamp; only events at or before this time.
        q: Free-text search across description, action, and resource_type
            (matches the audit page's search box).

    Requires the 'audit:read' permission.
    """
    if err := _check_permission("list_audit_events"):
        return err
    # Clamp limit to the server's accepted range; the server also caps at 500.
    if limit < 1:
        limit = 1
    elif limit > 500:
        limit = 500
    if offset < 0:
        offset = 0
    events = await _call_api(
        "list_audit_events",
        limit=limit,
        offset=offset,
        action=action,
        resource_type=resource_type,
        user_id=user_id,
        since=since,
        until=until,
        q=q,
    )
    return _json_result(events)


# ── Health check endpoints ──

async def reload_key_endpoint(request: Request) -> Response:
    """Signal the MCP server to reload its service key from Key Vault/file."""
    if request.method != "POST":
        return Response(status_code=405)
    await _reload_service_key()
    has_key = bool(_service_key)
    return JSONResponse({"reloaded": True, "has_key": has_key})


async def health_endpoint(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def health_api_endpoint(request: Request) -> Response:
    """Check if the MCP server can reach the CMS REST API using the service key."""
    try:
        if not _service_key:
            return JSONResponse(
                {"status": "warning", "detail": "Service key not loaded — waiting for CMS to provision"},
                status_code=200,
            )
        client = CMSClient(base_url=CMS_BASE_URL, api_key=_service_key)
        devices = await client.list_devices()
        return JSONResponse({"status": "ok", "device_count": len(devices)})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=502)


# ── Bearer token auth middleware ──

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validate bearer tokens (user API keys) against the CMS.

    On success, sets contextvars with the user's API key and permissions
    so MCP tools can create per-user CMSClients and pre-check permissions.
    """

    async def dispatch(self, request: Request, call_next):
        # Skip auth for health checks and OAuth discovery probes.
        # Returning 404 on /.well-known/ tells MCP clients that this
        # server uses simple Bearer auth, not OAuth.
        if request.url.path.startswith("/health") or request.url.path.startswith("/.well-known") or request.url.path == "/reload-key":
            return await call_next(request)

        token = request.headers.get("authorization", "")
        if not token:
            return JSONResponse(
                {"error": "Missing Authorization header"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Extract raw key from "Bearer <key>"
        raw_key = token.removeprefix("Bearer ").strip()

        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.get(
                    f"{CMS_BASE_URL}/api/mcp/auth",
                    headers={"Authorization": token},
                )
                if resp.status_code != 200:
                    detail = resp.json().get("detail", "Access denied")
                    return JSONResponse(
                        {"error": detail},
                        status_code=resp.status_code,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                auth_data = resp.json()
        except Exception as e:
            logger.error("Failed to validate token with CMS: %s", e)
            return JSONResponse(
                {"error": "Auth service unavailable"},
                status_code=503,
            )

        # Store user context for MCP tools
        _ctx_api_key.set(raw_key)
        _ctx_permissions.set(auth_data.get("permissions", []))
        _ctx_user_name.set(auth_data.get("user", "unknown"))

        logger.info(
            "Authenticated MCP user: %s (role: %s, %d permissions)",
            auth_data.get("user"),
            auth_data.get("role"),
            len(auth_data.get("permissions", [])),
        )

        return await call_next(request)


# ── Service key management ──


def _read_key_from_keyvault(vault_uri: str) -> str:
    """Read the MCP service key from Azure Key Vault.

    Returns the key value, or empty string on failure.
    """
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=vault_uri, credential=credential)
        secret = client.get_secret("mcp-service-key")
        return (secret.value or "").strip()
    except Exception as exc:
        logger.warning("Failed to read service key from Key Vault: %s", exc)
        return ""


def _load_service_key_sync() -> tuple[str, str]:
    """Read the service key from env var, Key Vault, or file (synchronous).

    Priority: SERVICE_KEY env var (direct injection) > Azure Key Vault > file.
    Returns (key, source) tuple.
    """
    # 1. Environment variable (direct injection via deploy scripts)
    env_key = os.environ.get("SERVICE_KEY", "").strip()
    if env_key:
        return env_key, "SERVICE_KEY env var"

    # 2. Azure Key Vault (managed identity in Azure Container Apps)
    if AZURE_KEYVAULT_URI:
        kv_key = _read_key_from_keyvault(AZURE_KEYVAULT_URI)
        if kv_key:
            return kv_key, f"Key Vault ({AZURE_KEYVAULT_URI})"

    # 3. Shared volume file (Docker Compose)
    try:
        path = Path(SERVICE_KEY_PATH)
        if path.exists():
            key = path.read_text().strip()
            if key:
                return key, SERVICE_KEY_PATH
    except Exception as e:
        logger.warning("Failed to read service key file: %s", e)

    return "", ""


async def _reload_service_key() -> None:
    """Reload the service key if it has changed."""
    global _service_key
    new_key, source = _load_service_key_sync()
    if new_key != _service_key:
        async with _service_key_lock:
            _service_key = new_key
        if new_key:
            logger.info("Service key loaded/reloaded from %s", source)
        else:
            logger.warning("Service key is empty or missing")


if __name__ == "__main__":
    # Load service key on startup (before server starts)
    _service_key, _source = _load_service_key_sync()
    if _service_key:
        logger.info("Service key loaded from %s", _source)
    else:
        logger.warning(
            "No service key found (checked: env var, Key Vault, %s) — MCP tools "
            "will fail until an admin enables MCP in the CMS Settings page.",
            SERVICE_KEY_PATH,
        )

    # Build the SSE app from FastMCP, then wrap it with auth
    sse_app = mcp.sse_app()

    async def lifespan(app):
        yield

    app = Starlette(
        routes=[
            Route("/health", health_endpoint),
            Route("/health/api", health_api_endpoint),
            Route("/reload-key", reload_key_endpoint, methods=["POST"]),
        ],
        middleware=[
            Middleware(BearerAuthMiddleware),
        ],
        lifespan=lifespan,
    )
    app.mount("/", sse_app)

    uvicorn.run(app, host="0.0.0.0", port=8000)
