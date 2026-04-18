"""Phase 6: schedules (#250).

Exercises the end-to-end schedule-playback flow against the real compose
stack:

1. Create a fresh group, assign 2 simulated devices
2. Pick a transcoded MP4 asset from Phase 3 (has ``duration_seconds``)
3. Create a schedule covering "all of today" targeting the group
4. Wait until CMS reports ``has_active_schedule`` → devices receive
   the sync, fake_player flips into PLAY and sends PLAYBACK_STARTED back
   over WebSocket
5. Verify `/api/dashboard` reports each member device in its `now_playing`
   array with the right schedule/asset
6. Rename the schedule via PATCH, verify dashboard reflects the new name
7. Delete the schedule, verify `has_active_schedule` clears on every member
   device and dashboard's `now_playing` empties for them
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.nightly.helpers.simulator import SimulatorClient


GROUP_NAME = "Nightly Schedule Group"
SCHEDULE_NAME_INITIAL = "Nightly All-Day Loop"
SCHEDULE_NAME_RENAMED = "Nightly All-Day Loop (renamed)"

# Generous — CMS scheduler eval runs every few seconds and device WS round-trip
# adds a second or two on top of that.
ACTIVATION_TIMEOUT_S = 60
POLL_INTERVAL_S = 1.0


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


def _pick_video_asset(page: Page) -> dict:
    """Return a transcoded video asset (has duration_seconds)."""
    # `/api/assets/status` returns variant state; `/api/assets` returns the
    # Asset record which carries duration_seconds / asset_type directly.
    assets = _api_get(page, "/api/assets")
    candidates = [
        a for a in assets
        if a.get("asset_type") == "video" and a.get("duration_seconds")
    ]
    assert candidates, (
        f"no video assets with duration available (got {len(assets)} total); "
        "Phase 3 should have uploaded an MP4"
    )
    return candidates[0]


def _poll_until(predicate, *, timeout: float = ACTIVATION_TIMEOUT_S,
                interval: float = POLL_INTERVAL_S, desc: str = "condition"):
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {desc} after {timeout}s (last={last!r})")


# ── tests ─────────────────────────────────────────────────────────────────


def test_create_schedule_group_and_assign_devices(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    page = authenticated_page

    # Drop any stale group/schedule from an aborted earlier run.
    for g in _api_get(page, "/api/devices/groups/"):
        if g["name"] == GROUP_NAME:
            # Detach any attached devices and delete in-order.
            for s in _api_get(page, "/api/schedules"):
                if s.get("group_id") == g["id"]:
                    _api_delete(page, f"/api/schedules/{s['id']}")
            dev_list = _api_get(page, "/api/devices")
            for d in dev_list:
                if d.get("group_id") == g["id"]:
                    _api_patch(page, f"/api/devices/{d['id']}", {"group_id": None})
            _api_delete(page, f"/api/devices/groups/{g['id']}")

    created = _api_post(
        page,
        "/api/devices/groups/",
        {"name": GROUP_NAME, "description": "Phase 6 schedule target"},
    )
    assert created["name"] == GROUP_NAME
    group_id = created["id"]

    serials = sorted(simulator.serials())
    assert len(serials) >= 2
    for serial in serials[:2]:
        updated = _api_patch(page, f"/api/devices/{serial}", {"group_id": group_id})
        assert updated["group_id"] == group_id, updated

    # Sanity: group now has 2 members.
    refreshed = next(
        g for g in _api_get(page, "/api/devices/groups/") if g["id"] == group_id
    )
    assert refreshed["device_count"] == 2, refreshed


def test_create_schedule_targeting_group(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    page = authenticated_page
    group = next(
        g for g in _api_get(page, "/api/devices/groups/") if g["name"] == GROUP_NAME
    )
    asset = _pick_video_asset(page)

    # 23h59m window starting at midnight → guaranteed active regardless of the
    # stack's configured timezone.
    body = {
        "name": SCHEDULE_NAME_INITIAL,
        "group_id": group["id"],
        "asset_id": asset["id"],
        "start_time": "00:00:00",
        "end_time": "23:59:00",
        "priority": 0,
        "enabled": True,
    }
    schedule = _api_post(page, "/api/schedules", body)
    assert schedule["name"] == SCHEDULE_NAME_INITIAL
    assert schedule["group_id"] == group["id"]
    assert schedule["asset_id"] == asset["id"]
    assert schedule["enabled"] is True

    # Listing should include it.
    listing = _api_get(page, "/api/schedules")
    ids = {s["id"] for s in listing}
    assert schedule["id"] in ids, listing


def test_schedule_activates_on_member_devices(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    page = authenticated_page
    group = next(
        g for g in _api_get(page, "/api/devices/groups/") if g["name"] == GROUP_NAME
    )
    member_serials = sorted(simulator.serials())[:2]

    # Poll each member until CMS flips has_active_schedule=True. That happens
    # after the scheduler push + device WS sync ack.
    for serial in member_serials:
        def _active(serial=serial):
            dev = _api_get(page, f"/api/devices/{serial}")
            return dev if dev.get("has_active_schedule") else None

        dev = _poll_until(
            _active, desc=f"{serial} has_active_schedule=True"
        )
        assert dev["group_id"] == group["id"], dev

    # Dashboard now_playing is populated when each device sends PLAYBACK_STARTED
    # back to the CMS (fake_player → cms_client → WS). This is the real e2e
    # acceptance check — the per-device `playback_asset` field is a secondary
    # live-state detail that only surfaces via heartbeat status pings.
    def _dashboard_ready():
        d = _api_get(page, "/api/dashboard")
        playing_ids = {
            np["device_id"] for np in d.get("now_playing", [])
            if np.get("schedule_name") == SCHEDULE_NAME_INITIAL
        }
        return d if set(member_serials).issubset(playing_ids) else None

    dash = _poll_until(_dashboard_ready, desc="dashboard now_playing populated")
    names = {np["schedule_name"] for np in dash["now_playing"]}
    assert SCHEDULE_NAME_INITIAL in names, dash["now_playing"]


def test_rename_schedule_reflects_in_dashboard(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    page = authenticated_page
    schedules = _api_get(page, "/api/schedules")
    sched = next(s for s in schedules if s["name"] == SCHEDULE_NAME_INITIAL)

    renamed = _api_patch(
        page,
        f"/api/schedules/{sched['id']}",
        {"name": SCHEDULE_NAME_RENAMED},
    )
    assert renamed["name"] == SCHEDULE_NAME_RENAMED

    # Dashboard should eventually reflect the new name for both devices.
    member_serials = sorted(simulator.serials())[:2]

    def _renamed():
        d = _api_get(page, "/api/dashboard")
        ids_with_new = {
            np["device_id"] for np in d.get("now_playing", [])
            if np.get("schedule_name") == SCHEDULE_NAME_RENAMED
        }
        return d if set(member_serials).issubset(ids_with_new) else None

    _poll_until(_renamed, desc="dashboard picks up schedule rename")


def test_delete_schedule_stops_playback(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    page = authenticated_page
    schedules = _api_get(page, "/api/schedules")
    sched = next(
        s for s in schedules
        if s["name"] in (SCHEDULE_NAME_RENAMED, SCHEDULE_NAME_INITIAL)
    )
    sched_id = sched["id"]

    deleted = _api_delete(page, f"/api/schedules/{sched_id}")
    assert deleted.get("deleted") == sched_id, deleted

    # Member devices should see has_active_schedule flip to False.
    member_serials = sorted(simulator.serials())[:2]
    for serial in member_serials:
        def _inactive(serial=serial):
            dev = _api_get(page, f"/api/devices/{serial}")
            return dev if not dev.get("has_active_schedule") else None

        _poll_until(
            _inactive, desc=f"{serial} has_active_schedule=False"
        )

    # Dashboard now_playing should no longer include our devices under the
    # deleted schedule.
    def _cleared():
        d = _api_get(page, "/api/dashboard")
        lingering = [
            np for np in d.get("now_playing", [])
            if np.get("schedule_id") == sched_id
        ]
        return d if not lingering else None

    _poll_until(_cleared, desc="dashboard now_playing clears deleted schedule")

    # Cleanup: detach devices from the group and drop the group so reruns are
    # clean.
    group = next(
        (g for g in _api_get(page, "/api/devices/groups/") if g["name"] == GROUP_NAME),
        None,
    )
    if group:
        for serial in member_serials:
            _api_patch(page, f"/api/devices/{serial}", {"group_id": None})
        _api_delete(page, f"/api/devices/groups/{group['id']}")
