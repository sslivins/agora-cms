"""Agora CMS MCP Server — exposes CMS operations as MCP tools over SSE."""

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

from cms_client import CMSClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CMS_BASE_URL = os.environ.get("CMS_BASE_URL", "http://cms:8080")

mcp = FastMCP("Agora CMS")
client = CMSClient()


def _json_result(data) -> str:
    """Format result as indented JSON for readability."""
    return json.dumps(data, indent=2, default=str)


# ── Devices ──


@mcp.tool()
async def list_devices() -> str:
    """List all registered Agora devices with their status, group, and connection state."""
    devices = await client.list_devices()
    return _json_result(devices)


@mcp.tool()
async def get_device(device_id: str) -> str:
    """Get detailed information about a specific device.

    Args:
        device_id: The device ID (Pi serial number or UUID).
    """
    device = await client.get_device(device_id)
    return _json_result(device)


@mcp.tool()
async def adopt_device(device_id: str) -> str:
    """Adopt a pending device so it can receive schedules and content.

    Args:
        device_id: The device ID to adopt.
    """
    result = await client.adopt_device(device_id)
    return _json_result(result)


@mcp.tool()
async def update_device(device_id: str, name: str | None = None, group_id: str | None = None) -> str:
    """Update a device's name or group assignment.

    Args:
        device_id: The device ID to update.
        name: New display name for the device.
        group_id: UUID of the group to assign the device to, or null to remove from group.
    """
    fields = {}
    if name is not None:
        fields["name"] = name
    if group_id is not None:
        fields["group_id"] = group_id
    result = await client.update_device(device_id, fields)
    return _json_result(result)


@mcp.tool()
async def reboot_device(device_id: str) -> str:
    """Send a reboot command to a device.

    Args:
        device_id: The device ID to reboot.
    """
    await client.reboot_device(device_id)
    return f"Reboot command sent to device {device_id}"


@mcp.tool()
async def delete_device(device_id: str) -> str:
    """Remove a device from the CMS. The device will need to be re-adopted.

    Args:
        device_id: The device ID to delete.
    """
    await client.delete_device(device_id)
    return f"Device {device_id} deleted"


# ── Groups ──


@mcp.tool()
async def list_groups() -> str:
    """List all device groups."""
    groups = await client.list_groups()
    return _json_result(groups)


@mcp.tool()
async def create_group(name: str, description: str = "") -> str:
    """Create a new device group for bulk scheduling.

    Args:
        name: Name of the group.
        description: Optional description.
    """
    result = await client.create_group(name, description)
    return _json_result(result)


@mcp.tool()
async def update_group(group_id: str, name: str | None = None, description: str | None = None) -> str:
    """Update a device group's name or description.

    Args:
        group_id: UUID of the group to update.
        name: New name for the group.
        description: New description.
    """
    fields = {}
    if name is not None:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    result = await client.update_group(group_id, fields)
    return _json_result(result)


@mcp.tool()
async def delete_group(group_id: str) -> str:
    """Delete a device group. Devices in the group will be ungrouped.

    Args:
        group_id: UUID of the group to delete.
    """
    await client.delete_group(group_id)
    return f"Group {group_id} deleted"


# ── Assets ──


@mcp.tool()
async def list_assets() -> str:
    """List all uploaded assets (videos and images) in the CMS library."""
    assets = await client.list_assets()
    return _json_result(assets)


@mcp.tool()
async def get_asset(asset_id: str) -> str:
    """Get detailed information about a specific asset.

    Args:
        asset_id: UUID of the asset.
    """
    asset = await client.get_asset(asset_id)
    return _json_result(asset)


@mcp.tool()
async def delete_asset(asset_id: str) -> str:
    """Delete an asset from the CMS library. Removes the file and all variants.

    Args:
        asset_id: UUID of the asset to delete.
    """
    await client.delete_asset(asset_id)
    return f"Asset {asset_id} deleted"


# ── Schedules ──


@mcp.tool()
async def list_schedules() -> str:
    """List all schedules with their target devices/groups, assets, and time windows."""
    schedules = await client.list_schedules()
    return _json_result(schedules)


@mcp.tool()
async def get_schedule(schedule_id: str) -> str:
    """Get detailed information about a specific schedule.

    Args:
        schedule_id: UUID of the schedule.
    """
    schedule = await client.get_schedule(schedule_id)
    return _json_result(schedule)


@mcp.tool()
async def create_schedule(
    name: str,
    asset_id: str,
    start_time: str,
    end_time: str,
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

    Args:
        name: Schedule display name.
        asset_id: UUID of the asset to play.
        start_time: Start time in HH:MM:SS format (e.g. "08:00:00").
        end_time: End time in HH:MM:SS format (e.g. "17:00:00").
        device_id: Target device ID (provide this OR group_id).
        group_id: Target group UUID (provide this OR device_id).
        start_date: Optional start date in ISO format (e.g. "2026-04-10T00:00:00Z").
        end_date: Optional end date in ISO format.
        days_of_week: Optional list of ISO weekday numbers (1=Monday, 7=Sunday).
        priority: Schedule priority (higher wins when overlapping). Default 0.
        enabled: Whether the schedule is active. Default true.
        loop_count: Optional number of times to loop the asset (null = infinite).
    """
    data = {
        "name": name,
        "asset_id": asset_id,
        "start_time": start_time,
        "end_time": end_time,
        "priority": priority,
        "enabled": enabled,
    }
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
    fields = {}
    for key in ("name", "asset_id", "device_id", "group_id", "start_time",
                "end_time", "start_date", "end_date", "days_of_week",
                "priority", "enabled", "loop_count"):
        val = locals()[key]
        if val is not None:
            fields[key] = val
    result = await client.update_schedule(schedule_id, fields)
    return _json_result(result)


@mcp.tool()
async def delete_schedule(schedule_id: str) -> str:
    """Delete a schedule.

    Args:
        schedule_id: UUID of the schedule to delete.
    """
    await client.delete_schedule(schedule_id)
    return f"Schedule {schedule_id} deleted"


@mcp.tool()
async def end_schedule_now(schedule_id: str) -> str:
    """End a currently-playing schedule immediately. It will resume at its next scheduled time.

    Args:
        schedule_id: UUID of the schedule to end.
    """
    await client.end_schedule_now(schedule_id)
    return f"Schedule {schedule_id} ended for current occurrence"


# ── Dashboard ──


@mcp.tool()
async def get_dashboard() -> str:
    """Get the current dashboard state: what's playing now, upcoming schedules, device states, and alerts."""
    dashboard = await client.get_dashboard()
    return _json_result(dashboard)


# ── Health check endpoint (used by CMS to detect if container is running) ──

async def health_endpoint(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


# ── Bearer token auth middleware ──

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validate bearer tokens against the CMS /api/mcp/auth endpoint.

    Allows /health through without auth (used by CMS health check).
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)

        token = request.headers.get("authorization", "")
        if not token:
            return JSONResponse(
                {"error": "Missing Authorization header"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

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
        except Exception as e:
            logger.error("Failed to validate token with CMS: %s", e)
            return JSONResponse(
                {"error": "Auth service unavailable"},
                status_code=503,
            )

        return await call_next(request)


if __name__ == "__main__":
    # Build the SSE app from FastMCP, then wrap it with auth
    sse_app = mcp.sse_app()

    app = Starlette(
        routes=[
            Route("/health", health_endpoint),
        ],
        middleware=[
            Middleware(BearerAuthMiddleware),
        ],
    )
    app.mount("/", sse_app)

    uvicorn.run(app, host="0.0.0.0", port=8000)
