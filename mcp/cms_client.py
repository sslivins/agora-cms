"""HTTP client for Agora CMS REST API.

Authenticates using the MCP service key (X-API-Key header) and passes
the real user identity via X-On-Behalf-Of for audit logging.
"""

import logging

import httpx

logger = logging.getLogger(__name__)


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

    async def _delete(self, path: str) -> str:
        resp = await self._client.delete(path)
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
        self, device_id: str, services: list[str] | None = None, since: str = "24h",
    ) -> dict:
        params = {"since": since}
        if services:
            params["services"] = services
        return await self._post(f"/api/devices/{device_id}/logs", params)

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

    async def close(self) -> None:
        await self._client.aclose()
