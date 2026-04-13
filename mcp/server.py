"""Agora CMS MCP Server — exposes CMS operations as MCP tools over SSE."""

import contextvars
import json
import logging
import os

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
}


def _get_client() -> CMSClient:
    """Return a CMSClient using the current user's API key."""
    return CMSClient(base_url=CMS_BASE_URL, api_key=_ctx_api_key.get())


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
    devices = await _get_client().list_devices()
    return _json_result(devices)


@mcp.tool()
async def get_device(device_id: str) -> str:
    """Get detailed information about a specific device.

    Args:
        device_id: The device ID (Pi serial number or UUID).
    """
    if err := _check_permission("get_device"):
        return err
    device = await _get_client().get_device(device_id)
    return _json_result(device)


@mcp.tool()
async def adopt_device(device_id: str) -> str:
    """Adopt a pending device so it can receive schedules and content.

    Args:
        device_id: The device ID to adopt.
    """
    if err := _check_permission("adopt_device"):
        return err
    result = await _get_client().adopt_device(device_id)
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
    result = await _get_client().update_device(device_id, fields)
    return _json_result(result)


@mcp.tool()
async def reboot_device(device_id: str) -> str:
    """Send a reboot command to a device.

    Args:
        device_id: The device ID to reboot.
    """
    if err := _check_permission("reboot_device"):
        return err
    await _get_client().reboot_device(device_id)
    return f"Reboot command sent to device {device_id}"


@mcp.tool()
async def delete_device(device_id: str) -> str:
    """Remove a device from the CMS. The device will need to be re-adopted.

    Args:
        device_id: The device ID to delete.
    """
    if err := _check_permission("delete_device"):
        return err
    await _get_client().delete_device(device_id)
    return f"Device {device_id} deleted"


# ── Groups ──


@mcp.tool()
async def list_groups() -> str:
    """List all device groups."""
    if err := _check_permission("list_groups"):
        return err
    groups = await _get_client().list_groups()
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
    result = await _get_client().create_group(name, description, default_asset_id=default_asset_id)
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
    result = await _get_client().update_group(group_id, fields)
    return _json_result(result)


@mcp.tool()
async def delete_group(group_id: str) -> str:
    """Delete a device group. Devices in the group will be ungrouped.

    Args:
        group_id: UUID of the group to delete.
    """
    if err := _check_permission("delete_group"):
        return err
    await _get_client().delete_group(group_id)
    return f"Group {group_id} deleted"


# ── Assets ──


@mcp.tool()
async def list_assets() -> str:
    """List all uploaded assets (videos and images) in the CMS library."""
    if err := _check_permission("list_assets"):
        return err
    assets = await _get_client().list_assets()
    return _json_result(assets)


@mcp.tool()
async def get_asset(asset_id: str) -> str:
    """Get detailed information about a specific asset.

    Args:
        asset_id: UUID of the asset.
    """
    if err := _check_permission("get_asset"):
        return err
    asset = await _get_client().get_asset(asset_id)
    return _json_result(asset)


@mcp.tool()
async def delete_asset(asset_id: str) -> str:
    """Delete an asset from the CMS library. Removes the file and all variants.

    Args:
        asset_id: UUID of the asset to delete.
    """
    if err := _check_permission("delete_asset"):
        return err
    await _get_client().delete_asset(asset_id)
    return f"Asset {asset_id} deleted"


# ── Schedules ──


@mcp.tool()
async def list_schedules() -> str:
    """List all schedules with their target devices/groups, assets, and time windows."""
    if err := _check_permission("list_schedules"):
        return err
    schedules = await _get_client().list_schedules()
    return _json_result(schedules)


@mcp.tool()
async def get_schedule(schedule_id: str) -> str:
    """Get detailed information about a specific schedule.

    Args:
        schedule_id: UUID of the schedule.
    """
    if err := _check_permission("get_schedule"):
        return err
    schedule = await _get_client().get_schedule(schedule_id)
    return _json_result(schedule)


@mcp.tool()
async def create_schedule(
    name: str,
    asset_id: str,
    start_time: str,
    end_time: str | None = None,
    device_id: str | None = None,
    group_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    days_of_week: list[int] | None = None,
    priority: int = 0,
    enabled: bool = True,
    loop_count: int | None = None,
) -> str:
    """Create a new playback schedule.

    Assigns an asset to play on a device or group during a time window.
    Either device_id or group_id must be provided (not both).

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
        end_time: End time in HH:MM:SS format. IGNORED when loop_count is set (auto-computed to start_time + loop_count × asset_duration).
        device_id: Target device ID (provide this OR group_id).
        group_id: Target group UUID (provide this OR device_id).
        start_date: Optional start date in ISO format (e.g. "2026-04-10T00:00:00Z").
        end_date: Optional end date in ISO format.
        days_of_week: Optional list of ISO weekday numbers (1=Monday, 7=Sunday).
        priority: Schedule priority (higher wins when overlapping). Default 0.
        enabled: Whether the schedule is active. Default true.
        loop_count: Number of times to loop the asset. When set, end_time is IGNORED and auto-computed. Omit or set null for infinite looping within the time window.
    """
    if err := _check_permission("create_schedule"):
        return err
    client = _get_client()
    data = {
        "name": name,
        "asset_id": asset_id,
        "start_time": start_time,
        "priority": priority,
        "enabled": enabled,
    }
    if end_time:
        data["end_time"] = end_time
    if device_id:
        data["device_id"] = device_id
    if group_id:
        data["group_id"] = group_id
    if start_date:
        data["start_date"] = start_date
    if end_date:
        data["end_date"] = end_date
    if days_of_week:
        data["days_of_week"] = days_of_week
    if loop_count is not None:
        data["loop_count"] = loop_count

    result = await client.create_schedule(data)
    return _json_result(result)


@mcp.tool()
async def update_schedule(
    schedule_id: str,
    name: str | None = None,
    asset_id: str | None = None,
    device_id: str | None = None,
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
        device_id: New target device ID.
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
    for key in ("name", "asset_id", "device_id", "group_id", "start_time",
                "end_time", "start_date", "end_date", "days_of_week",
                "priority", "enabled", "loop_count"):
        val = locals()[key]
        if val is not None:
            fields[key] = val
    result = await _get_client().update_schedule(schedule_id, fields)
    return _json_result(result)


@mcp.tool()
async def delete_schedule(schedule_id: str) -> str:
    """Delete a schedule.

    Args:
        schedule_id: UUID of the schedule to delete.
    """
    if err := _check_permission("delete_schedule"):
        return err
    await _get_client().delete_schedule(schedule_id)
    return f"Schedule {schedule_id} deleted"


@mcp.tool()
async def play_now(
    device_id: str,
    asset_id: str,
    name: str | None = None,
) -> str:
    """Immediately play an asset on a device. Creates a high-priority schedule
    for today with an all-day time window so playback starts right away.

    When done, call end_schedule_now with the returned schedule ID to stop
    playback, or delete_schedule to remove it entirely.

    Args:
        device_id: The device ID to play on.
        asset_id: UUID of the asset to play.
        name: Optional schedule name. Defaults to "<asset_filename> — Play Now".
    """
    if err := _check_permission("play_now"):
        return err
    client = _get_client()
    if not name:
        try:
            asset = await client.get_asset(asset_id)
            asset_name = asset.get("original_filename") or asset.get("filename") or asset_id
            name = f"{asset_name} \u2014 Play Now"
        except Exception:
            name = f"Play Now \u2014 {asset_id[:8]}"

    server_time = await client.get_server_time()
    today = server_time["local"][:10]
    data = {
        "name": name,
        "asset_id": asset_id,
        "device_id": device_id,
        "start_time": "00:00:00",
        "end_time": "23:59:00",
        "start_date": f"{today}T00:00:00Z",
        "end_date": f"{today}T23:59:59Z",
        "priority": 10,
        "enabled": True,
    }
    result = await client.create_schedule(data)
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
    await _get_client().end_schedule_now(schedule_id)
    return f"Schedule {schedule_id} ended for current occurrence"


# ── Profiles ──


@mcp.tool()
async def list_profiles() -> str:
    """List all transcode profiles (codec settings used when preparing video assets for devices)."""
    if err := _check_permission("list_profiles"):
        return err
    profiles = await _get_client().list_profiles()
    return _json_result(profiles)


# ── Logs ──


@mcp.tool()
async def get_device_logs(
    device_id: str,
    services: list[str] | None = None,
    since: str = "24h",
) -> str:
    """Request logs from a connected device via its WebSocket connection.

    Returns journalctl output from the device's agora services.
    The device must be online (connected via WebSocket).

    Args:
        device_id: ID of the device to get logs from.
        services: Optional list of systemd service names to filter (e.g. ["agora-player", "agora-api"]).
                  If omitted, returns logs from all agora services.
        since: Time range for logs (e.g. "1h", "24h", "7d"). Default "24h".
    """
    if err := _check_permission("get_device_logs"):
        return err
    result = await _get_client().request_device_logs(device_id, services=services, since=since)
    return _json_result(result)


# ── Server time ──


@mcp.tool()
async def get_server_time() -> str:
    """Get the CMS server's configured timezone and current local time.

    Use this before creating schedules to understand what timezone
    start_time and end_time will be interpreted in.
    """
    data = await _get_client().get_server_time()
    return _json_result(data)


# ── Dashboard ──


@mcp.tool()
async def get_dashboard() -> str:
    """Get the current dashboard state: what's playing now, upcoming schedules, device states, and alerts."""
    dashboard = await _get_client().get_dashboard()
    return _json_result(dashboard)


# ── Health check endpoints ──

async def health_endpoint(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def health_api_endpoint(request: Request) -> Response:
    """Check if the MCP server can reach the CMS REST API."""
    try:
        # Use env-configured API key for health checks
        api_key = os.environ.get("CMS_API_KEY", "")
        if not api_key:
            return JSONResponse(
                {"status": "warning", "detail": "CMS_API_KEY not configured"},
                status_code=200,
            )
        client = CMSClient(base_url=CMS_BASE_URL, api_key=api_key)
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
        if request.url.path.startswith("/health") or request.url.path.startswith("/.well-known"):
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


if __name__ == "__main__":
    # Build the SSE app from FastMCP, then wrap it with auth
    sse_app = mcp.sse_app()

    app = Starlette(
        routes=[
            Route("/health", health_endpoint),
            Route("/health/api", health_api_endpoint),
        ],
        middleware=[
            Middleware(BearerAuthMiddleware),
        ],
    )
    app.mount("/", sse_app)

    uvicorn.run(app, host="0.0.0.0", port=8000)
