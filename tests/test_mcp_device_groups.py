"""CMSClient coverage for Stage 6 device-group membership endpoints."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
def cms_client_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "mcp" / "cms_client.py"
    spec = importlib.util.spec_from_file_location("agora_mcp_cms_client_groups", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _client_with_mock(cms_client_module, handler):
    transport = httpx.MockTransport(handler)
    client = cms_client_module.CMSClient(base_url="http://cms:8080", api_key="k")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://cms:8080",
        transport=transport,
        headers={"X-API-Key": "k"},
    )
    return client


@pytest.mark.asyncio
class TestCMSClientDeviceGroups:
    async def test_update_device_passes_group_ids(self, cms_client_module):
        captured = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["method"] = req.method
            captured["path"] = req.url.path
            captured["body"] = req.content.decode("utf-8")
            return httpx.Response(200, json={"ok": True})

        client = await _client_with_mock(cms_client_module, handler)
        try:
            result = await client.update_device(
                "dev-1",
                {"group_ids": ["g-a", "g-b"]},
            )
        finally:
            await client.close()

        assert result == {"ok": True}
        assert captured["method"] == "PATCH"
        assert captured["path"] == "/api/devices/dev-1"
        assert '"group_ids":["g-a","g-b"]' in captured["body"]

    async def test_add_device_to_group_posts_expected_payload(self, cms_client_module):
        captured = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["method"] = req.method
            captured["path"] = req.url.path
            captured["body"] = req.content.decode("utf-8")
            return httpx.Response(200, json={"changed": True})

        client = await _client_with_mock(cms_client_module, handler)
        try:
            result = await client.add_device_to_group("dev-1", "group-1")
        finally:
            await client.close()

        assert result == {"changed": True}
        assert captured == {
            "method": "POST",
            "path": "/api/devices/dev-1/groups",
            "body": '{"group_id":"group-1"}',
        }

    async def test_remove_device_from_group_hits_delete_endpoint(self, cms_client_module):
        captured = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["method"] = req.method
            captured["path"] = req.url.path
            return httpx.Response(200, json={"changed": True, "group_ids": []})

        client = await _client_with_mock(cms_client_module, handler)
        try:
            result = await client.remove_device_from_group("dev-1", "group-1")
        finally:
            await client.close()

        assert result == {"changed": True, "group_ids": []}
        assert captured == {
            "method": "DELETE",
            "path": "/api/devices/dev-1/groups/group-1",
        }

    async def test_replace_device_groups_puts_expected_payload(self, cms_client_module):
        captured = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["method"] = req.method
            captured["path"] = req.url.path
            captured["body"] = req.content.decode("utf-8")
            return httpx.Response(200, json={"changed": True})

        client = await _client_with_mock(cms_client_module, handler)
        try:
            result = await client.replace_device_groups("dev-1", ["group-1", "group-2"])
        finally:
            await client.close()

        assert result == {"changed": True}
        assert captured == {
            "method": "PUT",
            "path": "/api/devices/dev-1/groups",
            "body": '{"group_ids":["group-1","group-2"]}',
        }
