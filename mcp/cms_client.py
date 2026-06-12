"""HTTP client for Agora CMS REST API.

Authenticates using the MCP service key (X-API-Key header) and passes
the real user identity via X-On-Behalf-Of for audit logging.
"""

import asyncio
import io
import logging
import tarfile

import httpx

logger = logging.getLogger(__name__)


def _extract_tar_gz(blob: bytes) -> dict[str, str]:
    """Extract a ``tar.gz`` bundle into ``{service_name: log_text}``.

    Strips any ``.log`` suffix so the output shape matches what the
    legacy synchronous RPC returned.
    """
    out: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            fh = tf.extractfile(member)
            if fh is None:
                continue
            name = member.name
            if name.endswith(".log"):
                name = name[:-4]
            out[name] = fh.read().decode("utf-8", errors="replace")
    return out


class CMSClient:
    """Thin async wrapper around the CMS REST API."""

    def __init__(
        self,
        base_url: str = "http://cms:8080",
        api_key: str = "",
        on_behalf_of: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key
        if on_behalf_of:
            headers["X-On-Behalf-Of"] = on_behalf_of
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=30.0, headers=headers,
        )

    async def _get(self, path: str) -> dict | list:
        resp = await self._client.get(path)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, json: dict | None = None) -> dict | list | str:
        resp = await self._client.post(path, json=json)
        resp.raise_for_status()
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    async def _patch(self, path: str, json: dict) -> dict:
        resp = await self._client.patch(path, json=json)
        resp.raise_for_status()
        return resp.json()

    async def _put(self, path: str, json: dict | None = None) -> dict:
        resp = await self._client.put(path, json=json)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str, *, params: dict | None = None) -> str:
        resp = await self._client.delete(path, params=params)
        resp.raise_for_status()
        return "ok"

    # ── Devices ──

    async def list_devices(self) -> list:
        return await self._get("/api/devices")

    async def get_device(self, device_id: str) -> dict:
        return await self._get(f"/api/devices/{device_id}")

    async def update_device(self, device_id: str, fields: dict) -> dict:
        return await self._patch(f"/api/devices/{device_id}", json=fields)

    async def adopt_device(self, device_id: str) -> dict:
        return await self._post(f"/api/devices/{device_id}/adopt")

    async def reboot_device(self, device_id: str) -> str:
        return await self._post(f"/api/devices/{device_id}/reboot")

    async def delete_device(self, device_id: str) -> str:
        return await self._delete(f"/api/devices/{device_id}")

    # ── Groups ──

    async def list_groups(self) -> list:
        return await self._get("/api/devices/groups/")

    async def create_group(
        self, name: str, description: str = "", *, default_asset_id: str | None = None,
    ) -> dict:
        data = {"name": name, "description": description}
        if default_asset_id is not None:
            data["default_asset_id"] = default_asset_id
        return await self._post("/api/devices/groups/", json=data)

    async def update_group(self, group_id: str, fields: dict) -> dict:
        return await self._patch(f"/api/devices/groups/{group_id}", json=fields)

    async def delete_group(self, group_id: str) -> str:
        return await self._delete(f"/api/devices/groups/{group_id}")

    # ── Assets ──

    async def list_assets(self) -> list:
        return await self._get("/api/assets")

    async def get_asset(self, asset_id: str) -> dict:
        return await self._get(f"/api/assets/{asset_id}")

    async def delete_asset(self, asset_id: str) -> str:
        return await self._delete(f"/api/assets/{asset_id}")

    async def create_webpage_asset(self, data: dict) -> dict:
        return await self._post("/api/assets/webpage", json=data)

    # ── Composed slides (AI editor) ──

    async def list_composed_widget_types(self) -> dict:
        return await self._get("/composed/widget-types")

    async def get_composed_layout(self, asset_id: str) -> dict:
        return await self._get(f"/composed/{asset_id}/layout")

    async def set_composed_widgets(self, asset_id: str, payload: dict) -> dict:
        return await self._put(
            f"/composed/{asset_id}/layout-friendly", json=payload
        )

    # ── Slideshows (AI editor) ──

    async def get_slideshow(self, asset_id: str) -> dict:
        return await self._get(f"/api/assets/{asset_id}/slides")

    async def set_slideshow_slides(self, asset_id: str, payload: dict) -> dict:
        return await self._put(
            f"/api/assets/{asset_id}/slides", json=payload
        )

    # ── Schedules ──

    async def list_schedules(self) -> list:
        return await self._get("/api/schedules")

    async def get_schedule(self, schedule_id: str) -> dict:
        return await self._get(f"/api/schedules/{schedule_id}")

    async def create_schedule(self, data: dict) -> dict:
        return await self._post("/api/schedules", json=data)

    async def update_schedule(self, schedule_id: str, fields: dict) -> dict:
        return await self._patch(f"/api/schedules/{schedule_id}", json=fields)

    async def delete_schedule(self, schedule_id: str) -> str:
        return await self._delete(f"/api/schedules/{schedule_id}")

    async def end_schedule_now(self, schedule_id: str) -> str:
        return await self._post(f"/api/schedules/{schedule_id}/end-now")

    # ── Profiles ──

    async def list_profiles(self) -> list:
        return await self._get("/api/profiles")

    # ── Logs ──

    async def request_device_logs(
        self,
        device_id: str,
        services: list[str] | None = None,
        since: str = "24h",
        *,
        poll_timeout: float = 60.0,
        poll_interval: float = 2.0,
    ) -> dict:
        """Create a log request, poll for completion, and return the logs.

        Under the hood this exercises the multi-replica-safe async flow:
        ``POST /api/logs/requests`` → poll ``GET .../{id}`` → ``GET
        .../{id}/download`` (which returns a ``tar.gz`` bundle).  The
        returned shape matches the legacy tool for callers that expected
        ``{service_name: log_text}``.

        If the device doesn't reply within ``poll_timeout`` seconds this
        returns ``{"request_id", "status", "last_error"}`` — the row is
        still live on the CMS and can be retrieved later.
        """
        body: dict = {"device_id": device_id, "since": since}
        if services:
            body["services"] = services

        created = await self._post("/api/logs/requests", body)
        if not isinstance(created, dict) or "request_id" not in created:
            raise RuntimeError(f"Unexpected create response: {created!r}")
        request_id = created["request_id"]

        deadline = asyncio.get_event_loop().time() + poll_timeout
        last: dict = {"request_id": request_id, "status": created.get("status")}
        while asyncio.get_event_loop().time() < deadline:
            row = await self._get(f"/api/logs/requests/{request_id}")
            if not isinstance(row, dict):
                raise RuntimeError(f"Unexpected status response: {row!r}")
            last = row
            status = row.get("status")
            if status == "ready":
                resp = await self._client.get(
                    f"/api/logs/requests/{request_id}/download",
                )
                resp.raise_for_status()
                return _extract_tar_gz(resp.content)
            if status == "failed":
                return {
                    "request_id": request_id,
                    "status": "failed",
                    "last_error": row.get("last_error"),
                }
            await asyncio.sleep(poll_interval)

        return {
            "request_id": request_id,
            "status": last.get("status", "pending"),
            "last_error": last.get("last_error"),
            "message": (
                "Device did not reply within "
                f"{poll_timeout:.0f}s — request is still live on the CMS."
            ),
        }

    # ── Dashboard ──

    async def get_dashboard(self) -> dict:
        return await self._get("/api/dashboard")

    # ── Server time ──

    async def get_server_time(self) -> dict:
        return await self._get("/api/server-time")

    # ── Audit log ──

    async def list_audit_events(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        action: str | None = None,
        resource_type: str | None = None,
        user_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        q: str | None = None,
    ) -> list:
        params: dict = {"limit": limit, "offset": offset}
        if action:
            params["action"] = action
        if resource_type:
            params["resource_type"] = resource_type
        if user_id:
            params["user_id"] = user_id
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if q:
            params["q"] = q
        resp = await self._client.get("/api/audit-log", params=params)
        resp.raise_for_status()
        return resp.json()

    # -- Devices: additional management actions --

    async def check_device_updates(self) -> dict:
        return await self._post("/api/devices/check-updates")

    async def set_device_password(self, device_id: str, password: str) -> dict:
        return await self._post(
            f"/api/devices/{device_id}/password", json={"password": password},
        )

    async def upgrade_device(self, device_id: str) -> dict:
        return await self._post(f"/api/devices/{device_id}/upgrade")

    async def toggle_device_ssh(self, device_id: str, enabled: bool) -> dict:
        return await self._post(
            f"/api/devices/{device_id}/ssh", json={"enabled": enabled},
        )

    async def factory_reset_device(self, device_id: str) -> dict:
        return await self._post(f"/api/devices/{device_id}/factory-reset")

    async def toggle_device_local_api(self, device_id: str, enabled: bool) -> dict:
        return await self._post(
            f"/api/devices/{device_id}/local-api", json={"enabled": enabled},
        )

    # -- Tags --

    async def list_tags(self) -> list:
        return await self._get("/api/tags")

    async def create_tag(self, name: str, color: str | None = None) -> dict:
        data: dict = {"name": name}
        if color is not None:
            data["color"] = color
        return await self._post("/api/tags", json=data)

    async def update_tag(self, tag_id: str, fields: dict) -> dict:
        return await self._patch(f"/api/tags/{tag_id}", json=fields)

    async def delete_tag(self, tag_id: str) -> str:
        return await self._delete(f"/api/tags/{tag_id}")

    # -- Saved asset views --

    async def list_asset_views(self) -> list:
        return await self._get("/api/asset-views")

    async def create_asset_view(
        self, name: str, filters: dict, is_default: bool = False,
    ) -> dict:
        return await self._post(
            "/api/asset-views",
            json={"name": name, "filters": filters, "is_default": is_default},
        )

    async def update_asset_view(self, view_id: str, fields: dict) -> dict:
        return await self._patch(f"/api/asset-views/{view_id}", json=fields)

    async def delete_asset_view(self, view_id: str) -> str:
        return await self._delete(f"/api/asset-views/{view_id}")

    # -- Assets: filtered listing + management --

    async def list_assets_paged(
        self,
        *,
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
    ) -> dict:
        params: list[tuple[str, str]] = []
        if q:
            params.append(("q", q))
        for t in type or []:
            params.append(("type", t))
        for gid in group_id or []:
            params.append(("group_id", gid))
        for uid in uploader_id or []:
            params.append(("uploader_id", uid))
        for tid in tag_id or []:
            params.append(("tag_id", tid))
        if uploaded_after:
            params.append(("uploaded_after", uploaded_after))
        if uploaded_before:
            params.append(("uploaded_before", uploaded_before))
        if usage:
            params.append(("usage", usage))
        params.append(("order", order))
        if cursor:
            params.append(("cursor", cursor))
        params.append(("page_size", str(page_size)))
        resp = await self._client.get("/api/assets/page", params=params)
        resp.raise_for_status()
        return resp.json()

    async def update_asset(self, asset_id: str, fields: dict) -> dict:
        return await self._patch(f"/api/assets/{asset_id}", json=fields)

    async def recapture_stream(self, asset_id: str) -> dict | str:
        return await self._post(f"/api/assets/{asset_id}/recapture")

    async def share_asset(self, asset_id: str, group_id: str) -> dict:
        resp = await self._client.post(
            f"/api/assets/{asset_id}/share", params={"group_id": group_id},
        )
        resp.raise_for_status()
        return resp.json()

    async def unshare_asset(self, asset_id: str, group_id: str) -> str:
        resp = await self._client.delete(
            f"/api/assets/{asset_id}/share", params={"group_id": group_id},
        )
        resp.raise_for_status()
        return "ok" if not resp.content else (
            resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
        )

    async def toggle_asset_global(self, asset_id: str) -> dict | str:
        return await self._post(f"/api/assets/{asset_id}/global")

    # -- Profiles: full CRUD --

    async def create_profile(self, data: dict) -> dict:
        return await self._post("/api/profiles", json=data)

    async def update_profile(self, profile_id: str, fields: dict) -> dict:
        return await self._put(f"/api/profiles/{profile_id}", json=fields)

    async def delete_profile(self, profile_id: str) -> str:
        return await self._delete(f"/api/profiles/{profile_id}")

    async def copy_profile(self, profile_id: str) -> dict:
        return await self._post(f"/api/profiles/{profile_id}/copy")

    async def reset_profile(self, profile_id: str) -> dict:
        return await self._post(f"/api/profiles/{profile_id}/reset")

    async def disable_profile(self, profile_id: str) -> dict:
        return await self._post(f"/api/profiles/{profile_id}/disable")

    async def enable_profile(self, profile_id: str) -> dict:
        return await self._post(f"/api/profiles/{profile_id}/enable")

    async def close(self) -> None:
        await self._client.aclose()
