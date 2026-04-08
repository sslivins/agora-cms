"""HTTP client for Agora CMS REST API.

Handles session-cookie authentication and provides typed methods
for every API operation the MCP tools expose.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

CMS_BASE_URL = os.environ.get("CMS_BASE_URL", "http://cms:8080")
CMS_USERNAME = os.environ.get("CMS_USERNAME", "admin")
CMS_PASSWORD = os.environ.get("CMS_PASSWORD", "agora")


class CMSClient:
    """Thin async wrapper around the CMS REST API."""

    def __init__(
        self,
        base_url: str = CMS_BASE_URL,
        username: str = CMS_USERNAME,
        password: str = CMS_PASSWORD,
    ):
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        self._authenticated = False

    async def _ensure_auth(self) -> None:
        """Login once and store the session cookie."""
        if self._authenticated:
            return
        resp = await self._client.post(
            "/login",
            data={"username": self._username, "password": self._password},
            follow_redirects=False,
        )
        if resp.status_code not in (200, 303):
            raise RuntimeError(f"CMS login failed: {resp.status_code}")
        self._authenticated = True
        logger.info("Authenticated with CMS at %s", self.base_url)

    async def _get(self, path: str) -> dict | list:
        await self._ensure_auth()
        resp = await self._client.get(path)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, json: dict | None = None) -> dict | list | str:
        await self._ensure_auth()
        resp = await self._client.post(path, json=json)
        resp.raise_for_status()
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    async def _patch(self, path: str, json: dict) -> dict:
        await self._ensure_auth()
        resp = await self._client.patch(path, json=json)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str) -> str:
        await self._ensure_auth()
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

    async def create_group(self, name: str, description: str = "") -> dict:
        return await self._post(
            "/api/devices/groups/",
            json={"name": name, "description": description},
        )

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

    async def close(self) -> None:
        await self._client.aclose()
