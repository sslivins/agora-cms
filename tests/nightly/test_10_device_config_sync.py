"""Phase 9b: per-device config changes trigger sync WS frames (#250).

test_05 already covers the schedule CRUD -> sync path. This module focuses
on the *device-scoped* mutations that cause the CMS to push a fresh
``SyncMessage`` to the device:

1. PATCH /api/devices/{id} {timezone: ...}
     -> sync with updated ``timezone`` field

2. PATCH /api/devices/{id} {default_asset_id: ...}
     -> fetch_asset command for the new default, then a sync carrying the
        resolved ``default_asset`` filename.

3. PATCH /api/devices/groups/{group_id} {default_asset_id: ...}
     -> fetch_asset + sync to every device in the group.

We drive the API directly (not the UI) because the target product surface
is the REST endpoints; the UI hooks each of these onto the same PATCH
calls (see static/app.js::setDeviceTimezone / setDefaultAsset).

Depends on:
- test_01_oobe (authenticated admin + post-OOBE creds)
- test_02_assets (at least one transcoded video asset uploaded)
- test_03_devices (all simulator devices adopted)
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.nightly.helpers.simulator import SimulatorClient


# A tz we can assume no other test has parked the device on. Valid IANA zone.
TARGET_TIMEZONE = "America/Halifax"
GROUP_NAME = "Nightly Config Sync Group"
SYNC_WAIT_S = 15.0


# ── helpers ──────────────────────────────────────────────────────────────


def _api_get(page: Page, path: str) -> Any:
    resp = page.request.get(path)
    assert resp.status == 200, f"GET {path} -> {resp.status}: {resp.text()[:400]}"
    return resp.json()


def _api_patch(page: Page, path: str, body: dict, *, expected: int = 200) -> Any:
    resp = page.request.patch(path, data=body)
    assert resp.status == expected, (
        f"PATCH {path} -> {resp.status} (expected {expected}): {resp.text()[:400]}"
    )
    return resp.json()


def _api_post(page: Page, path: str, body: dict, *, expected: int = 201) -> Any:
    resp = page.request.post(path, data=body)
    assert resp.status == expected, (
        f"POST {path} -> {resp.status} (expected {expected}): {resp.text()[:400]}"
    )
    return resp.json()


def _api_delete(page: Page, path: str, *, expected: int = 200) -> Any:
    resp = page.request.delete(path)
    assert resp.status == expected, (
        f"DELETE {path} -> {resp.status} (expected {expected}): {resp.text()[:400]}"
    )
    return resp.json() if resp.text() else {}


def _adopted_device_ids(page: Page) -> list[str]:
    return sorted(
        d["id"] for d in _api_get(page, "/api/devices")
        if d.get("status") == "adopted"
    )


def _wait_for_online(page: Page, device_id: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = _api_get(page, f"/api/devices/{device_id}")
        if last.get("is_online"):
            return
        time.sleep(0.5)
    raise AssertionError(f"device {device_id} not online after {timeout}s: {last!r}")


def _wait_for_sync(
    simulator: SimulatorClient,
    device_id: str,
    *,
    timeout: float = SYNC_WAIT_S,
    predicate=None,
) -> dict:
    """Poll the simulator recording until a ``sync`` command matching the
    predicate lands. Returns the matching sync payload.

    ``predicate`` receives the sync command's ``payload`` dict. If omitted,
    the first sync seen satisfies the wait.
    """
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        rec = simulator.get_recording(device_id)
        last = rec
        syncs = [c for c in rec.get("commands", []) if c.get("type") == "sync"]
        for cmd in reversed(syncs):
            payload = cmd.get("payload") or {}
            if predicate is None or predicate(payload):
                return cmd
        time.sleep(0.25)
    raise AssertionError(
        f"device {device_id} did not receive a matching sync within {timeout}s. "
        f"Last recording: {last!r}"
    )


def _pick_video_assets(page: Page, min_count: int = 2) -> list[dict]:
    assets = _api_get(page, "/api/assets")
    videos = [
        a for a in assets
        if a.get("asset_type") == "video" and a.get("duration_seconds")
    ]
    # Fall back to any asset with a filename if we don't have enough videos —
    # the config sync path only cares that we hand it a resolvable asset_id.
    if len(videos) < min_count:
        videos.extend(a for a in assets if a not in videos)
    assert len(videos) >= min_count, (
        f"need at least {min_count} assets for config sync tests; "
        f"got {len(assets)} total"
    )
    return videos[:min_count]


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def primary_device(authenticated_page: Page, simulator: SimulatorClient) -> str:
    simulator.wait_for_devices(expected_count=3, timeout=60.0)
    ids = _adopted_device_ids(authenticated_page)
    assert ids, "no adopted devices — test_03_devices must have run first"
    dev_id = ids[0]
    _wait_for_online(authenticated_page, dev_id)
    simulator.reset_recording(dev_id)
    return dev_id


@pytest.fixture
def config_sync_group(authenticated_page: Page) -> str:
    """A fresh group exclusive to this phase; torn down at session end."""
    page = authenticated_page
    groups = _api_get(page, "/api/devices/groups/")
    existing = next((g for g in groups if g["name"] == GROUP_NAME), None)
    if existing:
        # Detach members and drop so the test starts from a known state.
        for d in _api_get(page, "/api/devices"):
            if d.get("group_id") == existing["id"]:
                _api_patch(page, f"/api/devices/{d['id']}", {"group_id": None})
        _api_delete(page, f"/api/devices/groups/{existing['id']}")

    created = _api_post(
        page,
        "/api/devices/groups/",
        {"name": GROUP_NAME, "description": "Phase 9b config-sync fanout target"},
    )
    return created["id"]


# ── tests ────────────────────────────────────────────────────────────────


def test_device_timezone_change_triggers_sync_with_new_tz(
    authenticated_page: Page,
    simulator: SimulatorClient,
    primary_device: str,
) -> None:
    """PATCH /api/devices/{id} with a new timezone pushes a sync frame
    whose timezone field matches what was written."""
    page = authenticated_page
    device_id = primary_device

    before = _api_get(page, f"/api/devices/{device_id}")
    original_tz = before.get("timezone") or "UTC"
    target = TARGET_TIMEZONE if original_tz != TARGET_TIMEZONE else "Europe/Berlin"

    updated = _api_patch(page, f"/api/devices/{device_id}", {"timezone": target})
    assert updated["timezone"] == target

    sync = _wait_for_sync(
        simulator, device_id,
        predicate=lambda p: p.get("timezone") == target,
    )
    assert sync["payload"]["timezone"] == target

    # Revert so downstream phases see a normal baseline.
    _api_patch(page, f"/api/devices/{device_id}", {"timezone": original_tz})


def test_device_default_asset_change_triggers_fetch_and_sync(
    authenticated_page: Page,
    simulator: SimulatorClient,
    primary_device: str,
) -> None:
    """Setting a per-device default_asset_id sends fetch_asset + sync with
    default_asset populated."""
    page = authenticated_page
    device_id = primary_device

    videos = _pick_video_assets(page, min_count=1)
    asset = videos[0]

    before = _api_get(page, f"/api/devices/{device_id}")
    original_default = before.get("default_asset_id")

    try:
        updated = _api_patch(
            page, f"/api/devices/{device_id}",
            {"default_asset_id": asset["id"]},
        )
        assert str(updated["default_asset_id"]) == str(asset["id"])

        # The CMS pushes fetch_asset first (to stage the file on the device)
        # and then a sync carrying default_asset. Both are recorded.
        simulator.wait_for_command(device_id, "fetch_asset", timeout=SYNC_WAIT_S)

        def _has_default(payload: dict) -> bool:
            # SyncMessage.default_asset is the asset's canonical filename as
            # used on the device (see scheduler.build_device_sync).
            return bool(payload.get("default_asset"))

        sync = _wait_for_sync(simulator, device_id, predicate=_has_default)
        assert sync["payload"]["default_asset"], sync
    finally:
        # Restore prior default so cross-test state doesn't leak.
        _api_patch(
            page, f"/api/devices/{device_id}",
            {"default_asset_id": original_default},
        )


def test_group_default_asset_change_fans_out_sync_to_members(
    authenticated_page: Page,
    simulator: SimulatorClient,
    config_sync_group: str,
) -> None:
    """PATCH a group's default_asset_id → every member gets fetch_asset
    and a sync with matching default_asset."""
    page = authenticated_page
    group_id = config_sync_group

    # Pick two devices and assign them to our fresh group.
    serials = sorted(simulator.serials())
    assert len(serials) >= 2, "need at least 2 devices for group fanout test"
    members = serials[:2]
    for serial in members:
        _wait_for_online(page, serial)
        _api_patch(page, f"/api/devices/{serial}", {"group_id": group_id})

    # Reset recordings AFTER assignment so we only observe the fanout that
    # the group update triggers, not the assignment-time sync.
    for serial in members:
        simulator.reset_recording(serial)

    asset = _pick_video_assets(page, min_count=1)[0]
    updated = _api_patch(
        page, f"/api/devices/groups/{group_id}",
        {"default_asset_id": asset["id"]},
    )
    assert str(updated["default_asset_id"]) == str(asset["id"])

    for serial in members:
        simulator.wait_for_command(serial, "fetch_asset", timeout=SYNC_WAIT_S)
        sync = _wait_for_sync(
            simulator, serial,
            predicate=lambda p: bool(p.get("default_asset")),
        )
        assert sync["payload"]["default_asset"], (serial, sync)

    # Cleanup: unpin members and drop the group.
    for serial in members:
        _api_patch(page, f"/api/devices/{serial}", {"group_id": None})
    _api_delete(page, f"/api/devices/groups/{group_id}")
