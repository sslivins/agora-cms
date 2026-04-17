"""Phase 5: device groups (#250).

By now 3 simulator devices are adopted. This phase exercises the groups
API:

- Create a group via `POST /api/devices/groups/`
- Assign 2 of the 3 adopted devices into the group via
  `PATCH /api/devices/{id}` (`{group_id: <uuid>}`)
- Verify group listing shows `device_count=2`
- Verify each device's own record reports the new `group_name`
- Rename the group via PATCH, verify the rename propagates
- Pin the group's default asset to a transcoded Phase 3 asset and verify
  the group record reflects it
- Detach the devices (`group_id=None`) and DELETE the group; verify 404
  afterwards

All operations use the session cookie established by `authenticated_page`.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.nightly.helpers.simulator import SimulatorClient


GROUP_NAME_INITIAL = "Nightly Lobby Group"
GROUP_NAME_RENAMED = "Nightly Main Floor"
GROUP_DESC_INITIAL = "E2E smoke-test group created by test_04_groups"


# ── helpers ──────────────────────────────────────────────────────────────


def _api_get(page: Page, path: str) -> Any:
    resp = page.request.get(path)
    assert resp.status == 200, f"GET {path} -> {resp.status}: {resp.text()[:400]}"
    return resp.json()


def _api_post(page: Page, path: str, body: dict, *, expected: int = 201) -> Any:
    resp = page.request.post(path, data=body)
    assert resp.status == expected, (
        f"POST {path} -> {resp.status} (expected {expected}): {resp.text()[:400]}"
    )
    return resp.json()


def _api_patch(page: Page, path: str, body: dict) -> Any:
    resp = page.request.patch(path, data=body)
    assert resp.status == 200, f"PATCH {path} -> {resp.status}: {resp.text()[:400]}"
    return resp.json()


def _api_delete(page: Page, path: str, *, expected: int = 200) -> Any:
    resp = page.request.delete(path)
    assert resp.status == expected, (
        f"DELETE {path} -> {resp.status} (expected {expected}): {resp.text()[:400]}"
    )
    return resp.json() if resp.text() else {}


def _list_groups(page: Page) -> list[dict]:
    return _api_get(page, "/api/devices/groups/")


def _find_group(page: Page, name: str) -> dict | None:
    for g in _list_groups(page):
        if g["name"] == name:
            return g
    return None


def _healthy_asset_id(page: Page) -> str:
    """Return the id of an asset whose variants finished transcoding.

    Phase 3 left several assets behind; pick one deterministically.
    """
    assets = _api_get(page, "/api/assets")
    for asset in assets:
        if asset.get("transcoded") or asset.get("status") == "ready":
            return asset["id"]
    # Fall back to the newest one.
    assert assets, "no assets available — did Phase 3 run?"
    return assets[-1]["id"]


# ── tests ─────────────────────────────────────────────────────────────────


def test_create_group_via_api(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    page = authenticated_page

    # Drop any stale group left from an aborted earlier run (defensive).
    pre_existing = _find_group(page, GROUP_NAME_INITIAL)
    if pre_existing:
        _api_delete(page, f"/api/devices/groups/{pre_existing['id']}")

    created = _api_post(
        page,
        "/api/devices/groups/",
        {"name": GROUP_NAME_INITIAL, "description": GROUP_DESC_INITIAL},
    )
    assert created["name"] == GROUP_NAME_INITIAL
    assert created["description"] == GROUP_DESC_INITIAL
    assert created["device_count"] == 0
    assert created["default_asset_id"] in (None, "")

    # It should now appear in the listing.
    listed = _find_group(page, GROUP_NAME_INITIAL)
    assert listed, f"group {GROUP_NAME_INITIAL!r} missing from listing"
    assert listed["id"] == created["id"]


def test_assign_devices_to_group(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    page = authenticated_page
    group = _find_group(page, GROUP_NAME_INITIAL)
    assert group, "previous test did not create the group"

    serials = sorted(simulator.serials())
    assert len(serials) >= 2

    # Assign the first 2 of 3 adopted devices.
    assigned = serials[:2]
    untouched = serials[2]

    for serial in assigned:
        updated = _api_patch(page, f"/api/devices/{serial}", {"group_id": group["id"]})
        assert updated["group_id"] == group["id"], updated
        assert updated["group_name"] == GROUP_NAME_INITIAL, updated

    # List call should report device_count=2 now.
    refreshed = _find_group(page, GROUP_NAME_INITIAL)
    assert refreshed, refreshed
    assert refreshed["device_count"] == 2, refreshed

    # The untouched device should still have group_id=None.
    solo = _api_get(page, f"/api/devices/{untouched}")
    assert solo.get("group_id") in (None, ""), solo
    assert solo.get("group_name") in (None, ""), solo


def test_rename_group_propagates_to_devices(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    page = authenticated_page
    group = _find_group(page, GROUP_NAME_INITIAL)
    assert group, "initial group missing"

    renamed = _api_patch(
        page,
        f"/api/devices/groups/{group['id']}",
        {"name": GROUP_NAME_RENAMED},
    )
    assert renamed["name"] == GROUP_NAME_RENAMED
    assert renamed["device_count"] == 2

    # Old name should no longer match; new name should.
    assert _find_group(page, GROUP_NAME_INITIAL) is None
    assert _find_group(page, GROUP_NAME_RENAMED) is not None

    # Each member device's `group_name` should now reflect the new name.
    serials = sorted(simulator.serials())
    for serial in serials[:2]:
        d = _api_get(page, f"/api/devices/{serial}")
        assert d["group_name"] == GROUP_NAME_RENAMED, d


def test_set_group_default_asset(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    page = authenticated_page
    group = _find_group(page, GROUP_NAME_RENAMED)
    assert group, "renamed group missing"

    asset_id = _healthy_asset_id(page)

    updated = _api_patch(
        page,
        f"/api/devices/groups/{group['id']}",
        {"default_asset_id": asset_id},
    )
    assert updated["default_asset_id"] == asset_id, updated

    # Re-fetch via the list endpoint to confirm persistence.
    refreshed = _find_group(page, GROUP_NAME_RENAMED)
    assert refreshed is not None
    assert refreshed["default_asset_id"] == asset_id, refreshed


def test_delete_group_after_detaching_devices(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    page = authenticated_page
    group = _find_group(page, GROUP_NAME_RENAMED)
    assert group, "renamed group missing"
    group_id = group["id"]

    # Detach each device. PATCH with group_id=null clears the FK.
    serials = sorted(simulator.serials())
    for serial in serials[:2]:
        updated = _api_patch(page, f"/api/devices/{serial}", {"group_id": None})
        assert updated.get("group_id") in (None, ""), updated
        assert updated.get("group_name") in (None, ""), updated

    # device_count should drop to 0 before deletion is permitted.
    refreshed = _find_group(page, GROUP_NAME_RENAMED)
    assert refreshed is not None
    assert refreshed["device_count"] == 0, refreshed

    deleted = _api_delete(page, f"/api/devices/groups/{group_id}")
    assert deleted == {"deleted": group_id}, deleted

    # Confirm the group is gone.
    assert _find_group(page, GROUP_NAME_RENAMED) is None

    # GET the group directly — no per-group read endpoint exists on /groups/,
    # so we just assert it's not in the listing (already done above).
