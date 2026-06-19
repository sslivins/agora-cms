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
_ctx_on_behalf_of: contextvars.ContextVar[str] = contextvars.ContextVar("on_behalf_of", default="")

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
    "create_profile": "profiles:write",
    "update_profile": "profiles:write",
    "delete_profile": "profiles:write",
    "copy_profile": "profiles:write",
    "reset_profile": "profiles:write",
    "disable_profile": "profiles:write",
    "enable_profile": "profiles:write",
    "check_device_updates": "devices:manage",
    "set_device_password": "devices:manage",
    "upgrade_device": "devices:manage",
    "toggle_device_ssh": "devices:manage",
    "factory_reset_device": "devices:manage",
    "toggle_device_local_api": "devices:manage",
    "list_tags": "assets:read",
    "create_tag": "assets:write",
    "update_tag": "assets:write",
    "delete_tag": "assets:write",
    "tag_asset": "assets:write",
    "untag_asset": "assets:write",
    "list_asset_views": None,
    "create_asset_view": None,
    "update_asset_view": None,
    "delete_asset_view": None,
    "list_assets_paged": "assets:read",
    "update_asset": "assets:write",
    "recapture_stream": "assets:write",
    "share_asset": "assets:write",
    "unshare_asset": "assets:write",
    "toggle_asset_global": "assets:write",
    "get_device_logs": "logs:read",
    "get_server_time": None,  # any authenticated user
    "get_dashboard": None,    # any authenticated user
    "list_audit_events": "audit:read",
    "list_composed_widget_types": "assets:read",
    "get_composed_layout": "assets:read",
    "set_composed_widgets": "assets:write",
    "get_slideshow": "assets:read",
    "set_slideshow_slides": "assets:write",
}


def _get_client() -> CMSClient:
    """Return a CMSClient using the service key and current user's identity."""
    # Prefer the on-behalf-of UUID so CMS can run the request under the
    # real caller's permissions; fall back to the display name for
    # personal-MCP-key auth (where no UUID is returned).
    obo = _ctx_on_behalf_of.get() or _ctx_user_name.get()
    return CMSClient(
        base_url=CMS_BASE_URL,
        api_key=_service_key,
        on_behalf_of=obo,
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
        status = exc.response.status_code
        if status != 401:
            # Surface the CMS-provided ``detail`` (e.g. an ACL message like
            # "A global slideshow can only reference global source assets …
            # Mark these global first.") instead of a bare "400 Bad Request".
            # Without this the LLM only ever sees the generic status line and
            # can't relay the actionable reason to the user.
            detail: str
            try:
                body = exc.response.json()
                detail = body.get("detail") if isinstance(body, dict) else None
            except Exception:
                detail = None
            if not detail:
                detail = (exc.response.text or "").strip() or exc.response.reason_phrase
            raise RuntimeError(f"CMS API error {status}: {detail}") from exc
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


# ── Composed slides (AI editor) ──


@mcp.tool()
async def list_composed_widget_types() -> str:
    """List the widget types available for building a composed slide.

    Returns each widget's slug (``type``), display name, config
    JSON-schema, default config, required fields, and a
    ``references_asset`` flag. Call this before placing widgets so you
    use real widget types and valid config keys. Widgets whose
    ``references_asset`` is true (image, media) need a real ``asset_id``
    from ``list_assets``.
    """
    if err := _check_permission("list_composed_widget_types"):
        return err
    result = await _call_api("list_composed_widget_types")
    return _json_result(result)


@mcp.tool()
async def get_composed_layout(asset_id: str) -> str:
    """Get the current draft layout of a composed slide.

    Returns the slide's background color, the locked canvas/grid, and
    the placed widgets in friendly form
    (``{id, type, row, col, rowspan, colspan, config}``). When editing
    an existing slide, pass each widget's ``id`` back to
    ``set_composed_widgets`` to preserve its identity.

    Args:
        asset_id: UUID of the composed-slide asset being edited.
    """
    if err := _check_permission("get_composed_layout"):
        return err
    result = await _call_api("get_composed_layout", asset_id)
    return _json_result(result)


@mcp.tool()
async def set_composed_widgets(
    asset_id: str,
    widgets: list[dict],
    background_color: str | None = None,
) -> str:
    """Replace the widgets on a composed-slide draft.

    This writes the draft directly (the canvas is on a fixed 8-row x
    12-column grid, 1-indexed and inclusive). The human reviews and
    clicks Publish themselves — this tool never publishes. ``widgets``
    is the FULL replacement list, so include every widget you want kept.

    Each widget is ``{type, row, col, rowspan?, colspan?, config?,
    id?}``:
      - ``type``: a slug from ``list_composed_widget_types``.
      - ``row``/``col``: top-left cell (row 1-8, col 1-12).
      - ``rowspan``/``colspan``: default 1; must stay within the grid.
      - ``config``: the widget's config (see its config_schema).
      - ``id``: omit for a new widget; pass the existing id (from
        ``get_composed_layout``) to keep a widget's identity on edit.

    On invalid input the call returns structured per-widget errors —
    fix and retry. Args:
        asset_id: UUID of the composed-slide asset being edited.
        widgets: full replacement list of friendly widget objects.
        background_color: optional slide background hex (e.g. "#101010").
    """
    if err := _check_permission("set_composed_widgets"):
        return err
    payload: dict = {"widgets": widgets}
    if background_color is not None:
        payload["background_color"] = background_color
    result = await _call_api("set_composed_widgets", asset_id, payload)
    return _json_result(result)


@mcp.tool()
async def get_slideshow(asset_id: str) -> str:
    """Get the ordered slides of a slideshow asset.

    Each slide has a ``kind``: ``"asset"`` (a static slide pinning one
    asset) or ``"tag"`` (a *dynamic tag block* that expands at play time
    to every asset carrying a tag). Shared fields on every slide:
    ``{id, position, kind, duration_ms, play_to_end, transition,
    transition_ms, member_transition, member_transition_ms, fit,
    effect, effect_direction}``.

    ``asset`` slides also carry ``{source_asset_id, source_filename,
    source_asset_type, source_duration_seconds, thumbnail_url}`` (the
    tag fields are null). ``tag`` slides also carry ``{tag_id, tag_name,
    tag_order_by, member_count}`` (the source fields are null);
    ``member_count`` is how many assets currently match the tag.

    ``fit`` is one of ``cover`` / ``contain`` / ``contain_blur`` and
    ``effect`` is one of ``none`` / ``ken_burns`` (see
    ``set_slideshow_slides`` for their meanings).

    For ``asset`` slides, ``source_asset_id`` is the IMAGE / VIDEO /
    COMPOSED asset shown — pass these back to ``set_slideshow_slides`` to
    keep the same members; for ``tag`` slides pass back ``tag_id``. Call
    this before editing so you preserve the existing order and timing.

    Args:
        asset_id: UUID of the slideshow asset being edited.
    """
    if err := _check_permission("get_slideshow"):
        return err
    result = await _call_api("get_slideshow", asset_id)
    return _json_result(result)


@mcp.tool()
async def set_slideshow_slides(asset_id: str, slides: list[dict]) -> str:
    """Replace the ordered slides of a slideshow.

    ``slides`` is the FULL replacement list in display order — include
    every slide you want kept; omitted slides are removed. This change is
    saved and goes LIVE immediately (slideshows have no draft/publish
    step).

    Each slide has a ``kind``: ``"asset"`` (default, a static slide) or
    ``"tag"`` (a dynamic tag block). ``kind`` may be omitted for asset
    slides.

    ASSET slides — ``{source_asset_id, duration_ms?, play_to_end?,
    transition?, transition_ms?, fit?, effect?, effect_direction?}``:
      - ``source_asset_id``: UUID of an IMAGE / VIDEO / COMPOSED asset
        (from ``list_assets`` or an existing slide's ``source_asset_id``).
      - ``duration_ms``: how long the slide shows, 500–3,600,000
        (default 7000). Ignored for videos when ``play_to_end`` is true.
      - ``play_to_end``: for video slides, play the whole clip instead of
        using ``duration_ms`` (default false; only valid for video).
      - ``transition``: how this slide enters — one of ``cut``, ``fade``,
        ``fade_black``, ``dissolve``, ``push``, ``wipe``, ``zoom``
        (default ``cut``).
      - ``transition_ms``: transition length in ms, 0–5000 (default 600).
      - ``fit``: how the asset fills the screen — ``cover`` (fill and
        crop, no bars; default), ``contain`` (whole frame with black
        letterbox bars), or ``contain_blur`` (whole frame with the bars
        filled by a blurred zoomed copy of the image instead of black).
      - ``effect``: optional motion — ``none`` (static; default) or
        ``ken_burns`` (slow pan-and-zoom). Applies to image / composed
        slides; videos play their own motion.
      - ``effect_direction``: the Ken Burns motion path (only meaningful
        when ``effect`` is ``ken_burns``). A zoom — ``in`` or ``out`` —
        optionally combined with a pan direction: ``up``, ``down``,
        ``left``, ``right``, or a diagonal (``up_left``, ``up_right``,
        ``down_left``, ``down_right``). Examples: ``in`` (zoom in, no
        pan), ``out_down_right`` (zoom out drifting toward the
        bottom-right). Word order and separators DON'T matter —
        ``"zoom out right down"`` and ``"out-right-down"`` are both
        accepted and normalized to ``out_down_right``. Defaults to ``in``.

    TAG slides (dynamic blocks) — set ``kind: "tag"`` and ``tag_id``
    instead of ``source_asset_id``. A tag block expands at play time to
    every non-deleted asset carrying that tag, so it stays in sync as you
    tag/untag assets (use ``tag_asset`` / ``untag_asset`` to manage
    membership). Rules and extra fields:
      - ``tag_id``: UUID of the tag (from ``list_tags`` / ``create_tag``).
        Required; a tag slide must NOT carry ``source_asset_id``.
      - ``play_to_end`` is NOT allowed on a tag slide (a dynamic block
        has no single member to play to its natural end).
      - ``tag_order_by``: order of expanded members — only ``"tagged_at"``
        (default) is supported.
      - The playback fields above (``duration_ms``, ``transition``,
        ``transition_ms``, ``fit``, ``effect``, ``effect_direction``) act
        as deck-defaults that EVERY expanded member inherits.
      - VIDEO members of a tag block automatically play their full
        natural length; ``duration_ms`` only governs image/composed
        members. (You don't set ``play_to_end`` for this — it's automatic.)
      - ``member_transition``: the transition used BETWEEN expanded
        members (one of the same transition names as ``transition``).
        ``transition`` is the transition INTO the block; this is the one
        between its members. Omit / null ⇒ inherit ``transition``.
      - ``member_transition_ms``: that transition's length in ms, 0–5000.
        Null ⇒ inherit ``transition_ms``. Both member fields are
        tag-only and ignored for asset slides.

    A slideshow can hold up to 50 slides. On invalid input the call
    returns structured errors — fix and retry.

    Args:
        asset_id: UUID of the slideshow asset being edited.
        slides: full ordered replacement list of slide objects.
    """
    if err := _check_permission("set_slideshow_slides"):
        return err
    payload: dict = {"slides": slides}
    result = await _call_api("set_slideshow_slides", asset_id, payload)
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

    CLARIFY BEFORE CALLING (LLM behaviour):
    Do not invent values for optional fields.  In particular, if the
    user did NOT explicitly mention:
      - end_time / loop_count → omit both for an open-ended slot;
        DO NOT pick a loop_count like 5 to "make it short".
      - end_date → omit it (open-ended).  Do not copy start_date.
      - days_of_week → omit it (= every day).  Do not pick today's
        weekday on your own.
      - priority → omit it (defaults to 0).  Never pick 10 or any
        other non-zero value without being asked.
    If the request is ambiguous (e.g. "show this for a bit"), ASK the
    user rather than guessing — the approval card shows the literal
    args you chose, and a user who sees an unexpected loop_count or
    priority will reject the call.

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

    CLARIFY BEFORE CALLING (LLM behaviour):
    Pass ONLY the fields the user explicitly asked to change.  Do not
    re-send every field with its current value — anything you pass
    will be written, which means an off-by-one (e.g. "fix the start
    time" → also resending priority: 5 when it was actually 0) will
    silently overwrite unrelated config.  Read the schedule first
    with get_schedule if you need to know its current values.

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

    PREFER THIS over create_schedule for ad-hoc / "just for now" / "show
    this for a bit" requests — the user almost certainly wants playback
    starting immediately on the current day, not a recurring schedule
    with a guessed loop_count, end_time, or priority.  Use create_schedule
    only when the user explicitly wants a recurring or future-dated
    schedule.

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


# ── Tags ──


@mcp.tool()
async def list_tags() -> str:
    """List all asset-library tags with per-tag asset counts.

    Tags are an admin-managed, org-flat vocabulary applied to assets in
    the library. Use this to discover the tag set before filtering
    list_assets_paged with tag_id.
    """
    if err := _check_permission("list_tags"):
        return err
    return _json_result(await _call_api("list_tags"))


@mcp.tool()
async def create_tag(name: str, color: str | None = None) -> str:
    """Create a new tag. Admin only.

    Args:
        name: Tag name (lowercased automatically, 1-64 chars, must be unique).
        color: Optional hex color (e.g. "#aabbcc" or "#abc"). Server default applied otherwise.
    """
    if err := _check_permission("create_tag"):
        return err
    return _json_result(await _call_api("create_tag", name, color=color))


@mcp.tool()
async def update_tag(
    tag_id: str, name: str | None = None, color: str | None = None,
) -> str:
    """Rename and/or recolor a tag. Admin only.

    Args:
        tag_id: UUID of the tag.
        name: New name (optional).
        color: New hex color (optional).
    """
    if err := _check_permission("update_tag"):
        return err
    fields: dict = {}
    if name is not None:
        fields["name"] = name
    if color is not None:
        fields["color"] = color
    if not fields:
        return "No fields provided to update."
    return _json_result(await _call_api("update_tag", tag_id, fields))


@mcp.tool()
async def delete_tag(tag_id: str) -> str:
    """Delete a tag. Removes the tag from every asset it was applied to. Admin only.

    Args:
        tag_id: UUID of the tag.
    """
    if err := _check_permission("delete_tag"):
        return err
    await _call_api("delete_tag", tag_id)
    return f"Tag {tag_id} deleted"


@mcp.tool()
async def tag_asset(tag_id: str, asset_ids: list[str]) -> str:
    """Apply a tag to one or more assets.

    This is how you populate a *dynamic tag block* in a slideshow: a tag
    block (a ``kind="tag"`` slide in ``set_slideshow_slides``) expands at
    play time to every asset carrying its tag, so to add an asset to a
    tag block you tag the asset here. Idempotent — re-tagging an
    already-tagged asset is a no-op.

    Get ``tag_id`` from ``list_tags`` (or ``create_tag``) and the asset
    UUIDs from ``list_assets`` / ``list_assets_paged``. Returns
    ``{succeeded, failed}`` (per-asset; a bad id fails just that id, not
    the batch).

    Args:
        tag_id: UUID of the tag to apply.
        asset_ids: UUIDs of the assets to tag (1-500).
    """
    if err := _check_permission("tag_asset"):
        return err
    return _json_result(await _call_api("tag_assets", tag_id, asset_ids))


@mcp.tool()
async def untag_asset(tag_id: str, asset_ids: list[str]) -> str:
    """Remove a tag from one or more assets.

    The inverse of ``tag_asset`` — removes the asset from any dynamic tag
    block built on this tag. Idempotent (removing an absent tag is a
    no-op). Returns ``{succeeded, failed}``.

    Args:
        tag_id: UUID of the tag to remove.
        asset_ids: UUIDs of the assets to untag (1-500).
    """
    if err := _check_permission("untag_asset"):
        return err
    return _json_result(await _call_api("untag_assets", tag_id, asset_ids))


# ── Saved asset views ──


@mcp.tool()
async def list_asset_views() -> str:
    """List the caller's saved asset-library filter views.

    A saved view stores a filter preset (type, tags, group, uploader,
    date range, sort order, etc.) for quick recall from the asset
    library toolbar. Views are per-user; you only see your own.
    """
    if err := _check_permission("list_asset_views"):
        return err
    return _json_result(await _call_api("list_asset_views"))


@mcp.tool()
async def create_asset_view(
    name: str,
    filters: dict | None = None,
    is_default: bool = False,
) -> str:
    """Create a saved asset view.

    Args:
        name: View name (1-80 chars, must be unique for the caller).
        filters: Filter snapshot. Supported keys: q, type, group_id,
            uploader_id, tag_id, usage ('used'|'unused'),
            uploaded_after, uploaded_before, date_days, order.
            Example: {"type": "video", "date_days": "1", "order": "-uploaded_at"}.
        is_default: When true, this view auto-applies on next visit to
            the asset library (and clears the default flag on other views).
    """
    if err := _check_permission("create_asset_view"):
        return err
    return _json_result(
        await _call_api("create_asset_view", name, filters or {}, is_default),
    )


@mcp.tool()
async def update_asset_view(
    view_id: str,
    name: str | None = None,
    filters: dict | None = None,
    is_default: bool | None = None,
) -> str:
    """Update a saved asset view's name, filters, and/or default flag.

    Args:
        view_id: UUID of the saved view (must be owned by the caller).
        name: New name (optional).
        filters: New filter snapshot (optional, replaces in full).
        is_default: When true, promote this view to default (auto-clears
            the flag on other views). When false, unset the default flag.
    """
    if err := _check_permission("update_asset_view"):
        return err
    fields: dict = {}
    if name is not None:
        fields["name"] = name
    if filters is not None:
        fields["filters"] = filters
    if is_default is not None:
        fields["is_default"] = is_default
    if not fields:
        return "No fields provided to update."
    return _json_result(await _call_api("update_asset_view", view_id, fields))


@mcp.tool()
async def delete_asset_view(view_id: str) -> str:
    """Delete a saved asset view.

    Args:
        view_id: UUID of the saved view (must be owned by the caller).
    """
    if err := _check_permission("delete_asset_view"):
        return err
    await _call_api("delete_asset_view", view_id)
    return f"View {view_id} deleted"


# ── Assets: filtered listing + management ──


@mcp.tool()
async def list_assets_paged(
    q: str | None = None,
    type: list[str] | None = None,
    group_id: list[str] | None = None,
    uploader_id: list[str] | None = None,
    tag_id: list[str] | None = None,
    uploaded_after: str | None = None,
    uploaded_before: str | None = None,
    usage: str | None = None,
    order: str = "-uploaded_at",
    cursor: str | None = None,
    page_size: int = 50,
) -> str:
    """Paginated + filtered asset listing (mirrors what the asset library UI uses).

    All filters AND-compose. ``q`` is case-insensitive substring match
    across display_name / original_filename / filename. Multiple values
    in type/group_id/uploader_id/tag_id are OR-combined within the
    field, except tag_id which requires ALL selected tags (AND).

    Args:
        q: Substring search.
        type: Restrict to types. Valid values: 'video', 'image',
            'webpage', 'stream', 'saved_stream', 'slideshow'.
        group_id: Restrict to assets shared with any of these groups.
        uploader_id: Restrict to assets uploaded by any of these users.
        tag_id: Restrict to assets carrying ALL these tags.
        uploaded_after: ISO-8601 lower bound on uploaded_at (inclusive).
        uploaded_before: ISO-8601 upper bound on uploaded_at (exclusive).
        usage: 'used' (referenced by a non-expired schedule or slideshow
            slide) or 'unused'.
        order: Sort key. Default '-uploaded_at'. Prefix with '-' for descending.
        cursor: Opaque pagination cursor returned in a previous response.
        page_size: 1-200, default 50.
    """
    if err := _check_permission("list_assets_paged"):
        return err
    return _json_result(await _call_api(
        "list_assets_paged",
        q=q, type=type, group_id=group_id, uploader_id=uploader_id,
        tag_id=tag_id, uploaded_after=uploaded_after,
        uploaded_before=uploaded_before, usage=usage, order=order,
        cursor=cursor, page_size=page_size,
    ))


@mcp.tool()
async def update_asset(
    asset_id: str,
    display_name: str | None = None,
    url: str | None = None,
) -> str:
    """Update editable asset properties.

    Args:
        asset_id: UUID of the asset.
        display_name: New display name (max 255 chars; pass empty string to clear).
        url: New URL (webpage assets only). Stream URLs cannot be edited
            mid-flight; use recapture_stream after editing a saved-stream URL.
    """
    if err := _check_permission("update_asset"):
        return err
    fields: dict = {}
    if display_name is not None:
        fields["display_name"] = display_name
    if url is not None:
        fields["url"] = url
    if not fields:
        return "No fields provided to update."
    return _json_result(await _call_api("update_asset", asset_id, fields))


@mcp.tool()
async def recapture_stream(asset_id: str) -> str:
    """Re-capture a SAVED_STREAM asset: redownload the stream, overwrite the
    capture, and reset all variants to PENDING for retranscoding.

    Args:
        asset_id: UUID of a saved-stream asset.
    """
    if err := _check_permission("recapture_stream"):
        return err
    return _json_result(await _call_api("recapture_stream", asset_id))


@mcp.tool()
async def share_asset(asset_id: str, group_id: str) -> str:
    """Share an asset with an additional group.

    Args:
        asset_id: UUID of the asset.
        group_id: UUID of the group to share with.
    """
    if err := _check_permission("share_asset"):
        return err
    return _json_result(await _call_api("share_asset", asset_id, group_id))


@mcp.tool()
async def unshare_asset(asset_id: str, group_id: str) -> str:
    """Remove an asset from a group.

    Returns 409 if a slideshow scoped to the group still references this
    asset (would orphan visibility); resolve by removing the source from
    the blocking slideshow(s) first.

    Args:
        asset_id: UUID of the asset.
        group_id: UUID of the group to unshare from.
    """
    if err := _check_permission("unshare_asset"):
        return err
    return _json_result(await _call_api("unshare_asset", asset_id, group_id))


@mcp.tool()
async def toggle_asset_global(asset_id: str) -> str:
    """Toggle an asset's global visibility (visible to all groups vs. only shared groups).

    Returns 409 when un-globalising would orphan an existing global
    slideshow that references the asset.

    Args:
        asset_id: UUID of the asset.
    """
    if err := _check_permission("toggle_asset_global"):
        return err
    return _json_result(await _call_api("toggle_asset_global", asset_id))


# ── Devices: additional management actions ──


@mcp.tool()
async def check_device_updates() -> str:
    """Trigger an immediate check for the latest device firmware version.

    Refreshes the CMS bundle_checker so device.available_version reflects
    the newest agora-os release without waiting for the periodic poll.
    """
    if err := _check_permission("check_device_updates"):
        return err
    return _json_result(await _call_api("check_device_updates"))


@mcp.tool()
async def set_device_password(device_id: str, password: str) -> str:
    """Set the device's local web admin password. Device must be online.

    Args:
        device_id: ID of the device.
        password: New web password (>= 4 chars).
    """
    if err := _check_permission("set_device_password"):
        return err
    return _json_result(
        await _call_api("set_device_password", device_id, password),
    )


@mcp.tool()
async def upgrade_device(device_id: str) -> str:
    """Tell a connected device to upgrade to its currently-available OS version.

    Returns 409 if the device is stuck mid-tryboot (must recover or be
    rebooted manually first). Use check_device_updates first if you
    suspect available_version is stale.

    Args:
        device_id: ID of the device.
    """
    if err := _check_permission("upgrade_device"):
        return err
    return _json_result(await _call_api("upgrade_device", device_id))


@mcp.tool()
async def toggle_device_ssh(device_id: str, enabled: bool) -> str:
    """Enable or disable SSH on a connected device.

    Args:
        device_id: ID of the device.
        enabled: True to enable SSH, False to disable.
    """
    if err := _check_permission("toggle_device_ssh"):
        return err
    return _json_result(
        await _call_api("toggle_device_ssh", device_id, enabled),
    )


@mcp.tool()
async def factory_reset_device(device_id: str) -> str:
    """Trigger a factory reset on a connected device. Destructive.

    Args:
        device_id: ID of the device.
    """
    if err := _check_permission("factory_reset_device"):
        return err
    return _json_result(await _call_api("factory_reset_device", device_id))


@mcp.tool()
async def toggle_device_local_api(device_id: str, enabled: bool) -> str:
    """Enable or disable the device's local HTTP API.

    Args:
        device_id: ID of the device.
        enabled: True to enable, False to disable.
    """
    if err := _check_permission("toggle_device_local_api"):
        return err
    return _json_result(
        await _call_api("toggle_device_local_api", device_id, enabled),
    )


# ── Profiles: full CRUD ──


@mcp.tool()
async def create_profile(
    name: str,
    description: str = "",
    video_codec: str = "h264",
    video_profile: str = "main",
    max_width: int = 1920,
    max_height: int = 1080,
    max_fps: int = 30,
    video_bitrate: str = "",
    crf: int = 23,
    pixel_format: str = "auto",
    color_space: str = "auto",
    audio_codec: str = "aac",
    audio_bitrate: str = "128k",
) -> str:
    """Create a new transcode profile.

    Args:
        name: 1-64 chars; letters, digits, hyphens, underscores; must
            start with a letter or digit. Becomes part of variant
            filenames so it's restricted to safe chars.
        description: Free-form description.
        video_codec: 'h264' | 'hevc' | 'av1' | ...
        video_profile: codec profile (e.g. 'main', 'high', 'main10').
        max_width, max_height: target resolution caps.
        max_fps: frame-rate cap.
        video_bitrate: bitrate string (e.g. '4M'); empty = CRF-driven.
        crf: 0-51 (lower = higher quality).
        pixel_format: 'auto' or e.g. 'yuv420p', 'yuv420p10le'.
        color_space: 'auto' or e.g. 'bt709', 'bt2020nc'. Must be
            compatible with video_profile (HDR color spaces require a
            10-bit profile).
        audio_codec: e.g. 'aac', 'opus'.
        audio_bitrate: e.g. '128k'.
    """
    if err := _check_permission("create_profile"):
        return err
    data = {
        "name": name, "description": description,
        "video_codec": video_codec, "video_profile": video_profile,
        "max_width": max_width, "max_height": max_height, "max_fps": max_fps,
        "video_bitrate": video_bitrate, "crf": crf,
        "pixel_format": pixel_format, "color_space": color_space,
        "audio_codec": audio_codec, "audio_bitrate": audio_bitrate,
    }
    return _json_result(await _call_api("create_profile", data))


@mcp.tool()
async def update_profile(
    profile_id: str,
    description: str | None = None,
    video_codec: str | None = None,
    video_profile: str | None = None,
    max_width: int | None = None,
    max_height: int | None = None,
    max_fps: int | None = None,
    video_bitrate: str | None = None,
    crf: int | None = None,
    pixel_format: str | None = None,
    color_space: str | None = None,
    audio_codec: str | None = None,
    audio_bitrate: str | None = None,
) -> str:
    """Update a transcode profile (PUT semantics with only the provided fields applied).

    Name is immutable. Built-in profiles can be edited but can be
    restored via reset_profile. Internal (non-device) profiles cannot
    be edited.

    Args:
        profile_id: UUID of the profile.
        (other args same shape as create_profile; all optional.)
    """
    if err := _check_permission("update_profile"):
        return err
    fields = {
        k: v for k, v in {
            "description": description, "video_codec": video_codec,
            "video_profile": video_profile, "max_width": max_width,
            "max_height": max_height, "max_fps": max_fps,
            "video_bitrate": video_bitrate, "crf": crf,
            "pixel_format": pixel_format, "color_space": color_space,
            "audio_codec": audio_codec, "audio_bitrate": audio_bitrate,
        }.items() if v is not None
    }
    if not fields:
        return "No fields provided to update."
    return _json_result(await _call_api("update_profile", profile_id, fields))


@mcp.tool()
async def delete_profile(profile_id: str) -> str:
    """Delete a transcode profile. Refused if any device is assigned to it.

    Cancels in-flight transcodes for the profile and removes all of its
    variants (both DB rows and files). Built-in and internal profiles
    cannot be deleted.

    Args:
        profile_id: UUID of the profile.
    """
    if err := _check_permission("delete_profile"):
        return err
    await _call_api("delete_profile", profile_id)
    return f"Profile {profile_id} deleted"


@mcp.tool()
async def copy_profile(profile_id: str) -> str:
    """Duplicate a profile (auto-named 'Copy of <name>', 'Copy of <name> (2)', ...).

    Args:
        profile_id: UUID of the source profile.
    """
    if err := _check_permission("copy_profile"):
        return err
    return _json_result(await _call_api("copy_profile", profile_id))


@mcp.tool()
async def reset_profile(profile_id: str) -> str:
    """Reset a built-in profile to its canonical default values.

    Only valid for built-in profiles. Cancels in-flight transcodes and
    supersedes existing variants if transcoding-relevant fields change.

    Args:
        profile_id: UUID of a built-in profile.
    """
    if err := _check_permission("reset_profile"):
        return err
    return _json_result(await _call_api("reset_profile", profile_id))


@mcp.tool()
async def disable_profile(profile_id: str) -> str:
    """Disable a profile. New variants stop being generated, pending/in-flight
    transcodes for this profile are cancelled. Existing READY variants are
    preserved so re-enabling is instant.

    Args:
        profile_id: UUID of the profile.
    """
    if err := _check_permission("disable_profile"):
        return err
    return _json_result(await _call_api("disable_profile", profile_id))


@mcp.tool()
async def enable_profile(profile_id: str) -> str:
    """Re-enable a profile. Re-runs the variant fan-out so assets uploaded
    while the profile was disabled get their variants enqueued.

    Args:
        profile_id: UUID of the profile.
    """
    if err := _check_permission("enable_profile"):
        return err
    return _json_result(await _call_api("enable_profile", profile_id))


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

        # Forward X-On-Behalf-Of unchanged.  When the CMS Assistant
        # calls MCP, it sends the service key as bearer AND this header
        # to identify which user the request is acting for; the CMS
        # auth endpoint validates the pairing and returns that user's
        # permissions.  For personal-MCP-key auth this header is unset
        # and harmlessly omitted.
        on_behalf_of = request.headers.get("x-on-behalf-of", "")
        forwarded_headers = {"Authorization": token}
        if on_behalf_of:
            forwarded_headers["X-On-Behalf-Of"] = on_behalf_of

        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.get(
                    f"{CMS_BASE_URL}/api/mcp/auth",
                    headers=forwarded_headers,
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

        # Store user context for MCP tools.  For service-key auth the
        # CMS echoes the resolved user via ``on_behalf_of``; for
        # personal-key auth it carries the key owner's display name.
        _ctx_api_key.set(raw_key)
        _ctx_permissions.set(auth_data.get("permissions", []))
        _ctx_user_name.set(auth_data.get("user", "unknown"))
        _ctx_on_behalf_of.set(auth_data.get("on_behalf_of", ""))

        logger.info(
            "Authenticated MCP user: %s (role: %s, %d permissions, auth=%s)",
            auth_data.get("user"),
            auth_data.get("role"),
            len(auth_data.get("permissions", [])),
            auth_data.get("key_type", "mcp"),
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
