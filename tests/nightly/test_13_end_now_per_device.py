"""Phase 13: Per-device "End Now" regression smoke (#415).

Validates the fix from PR #415: when an operator clicks "End Now" on a live
schedule from a per-device row in the dashboard, the schedule must:

1. Disappear from that device's ``now_playing`` list, AND
2. Disappear from the dashboard's ``upcoming`` list (no stuck "Starting…"
   badge), provided that all adopted targets of the schedule are skipped.

Pre-fix behaviour: the schedule re-appeared in ``upcoming`` with a
"starting" badge until its window ended, because the upcoming computation
only honoured schedule-wide skips, not per-device skips.

This test exercises the SINGLE-TARGET case, which is the simplest valid
trigger for the "Coming Up disappears" regression. A separate scope test
(per-device end-now hides the schedule for A but B keeps playing) would
require a fresh 2-device group and is intentionally out of scope here —
the unit tests in ``tests/test_per_device_skip_upcoming.py`` already
cover both shapes.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.nightly.helpers.simulator import SimulatorClient


# Unique-per-run names so concurrent / leftover state from prior runs
# can't mask a real failure.  Group/schedule names share the prefix so
# pre-clean is straightforward.
PREFIX = "Phase 13 EndNow Smoke"
RUN_ID = uuid.uuid4().hex[:8]
GROUP_NAME = f"{PREFIX} {RUN_ID}"
SCHEDULE_NAME = f"{PREFIX} sched {RUN_ID}"

ACTIVATION_TIMEOUT_S = 60
TEARDOWN_TIMEOUT_S = 60
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


def _api_post_json(page: Page, path: str, body: dict, *, expected: int = 200) -> Any:
    """Force a JSON body — used for ``end-now`` so a content-type slip
    can never silently fall through to schedule-wide end-now."""
    resp = page.request.post(
        path,
        data=json.dumps(body),
        headers={"content-type": "application/json"},
    )
    assert resp.status == expected, (
        f"POST {path} -> {resp.status} (expected {expected}): {resp.text()[:400]}"
    )
    return resp.json()


def _api_patch(page: Page, path: str, body: dict) -> Any:
    resp = page.request.patch(path, data=body)
    assert resp.status == 200, f"PATCH {path} -> {resp.status}: {resp.text()[:400]}"
    return resp.json()


def _api_delete(page: Page, path: str, *, allow: tuple[int, ...] = (200, 204, 404)) -> None:
    resp = page.request.delete(path)
    assert resp.status in allow, (
        f"DELETE {path} -> {resp.status} (allowed {allow}): {resp.text()[:400]}"
    )


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


def _pick_video_asset(page: Page) -> dict:
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


def _adopted_serials(page: Page) -> list[str]:
    return sorted(
        d["id"] for d in _api_get(page, "/api/devices")
        if d.get("status") == "adopted"
    )


def _preclean_by_prefix(page: Page) -> None:
    """Remove any leftover groups/schedules from earlier aborted runs.

    Anything matching the PREFIX is fair game — the suffix is unique
    per run so we only purge true leftovers, not concurrent runs.
    """
    for s in _api_get(page, "/api/schedules"):
        if isinstance(s.get("name"), str) and s["name"].startswith(PREFIX):
            _api_delete(page, f"/api/schedules/{s['id']}")
    for g in _api_get(page, "/api/devices/groups/"):
        if isinstance(g.get("name"), str) and g["name"].startswith(PREFIX):
            for d in _api_get(page, "/api/devices"):
                if d.get("group_id") == g["id"]:
                    _api_patch(page, f"/api/devices/{d['id']}", {"group_id": None})
            _api_delete(page, f"/api/devices/groups/{g['id']}")


# ── test ─────────────────────────────────────────────────────────────────


def test_per_device_end_now_clears_coming_up(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    """End Now on a single-target schedule must hide it from ``upcoming``.

    Pre-fix this was the visible regression: after End Now the schedule
    flipped from ``now_playing`` back to ``upcoming`` with a "starting"
    badge, persisting until the schedule's natural end_time.
    """
    page = authenticated_page
    _preclean_by_prefix(page)

    # Pick one adopted device — the regression only fires when *every*
    # adopted target gets skipped, so single-target is the cleanest signal.
    serials = _adopted_serials(page)
    assert serials, "no adopted devices — test_03 must have run first"
    target_serial = serials[0]

    # Capture original group so we can restore it in finally.
    orig = next(d for d in _api_get(page, "/api/devices") if d["id"] == target_serial)
    orig_group_id = orig.get("group_id")

    asset = _pick_video_asset(page)
    schedule_id: str | None = None
    group_id: str | None = None

    try:
        # Fresh single-member group.
        group = _api_post(
            page,
            "/api/devices/groups/",
            {"name": GROUP_NAME, "description": "PR #415 regression smoke"},
        )
        group_id = group["id"]
        _api_patch(page, f"/api/devices/{target_serial}", {"group_id": group_id})

        # Verify exactly one ADOPTED target — the assertion the fix relies on.
        refreshed = next(
            g for g in _api_get(page, "/api/devices/groups/") if g["id"] == group_id
        )
        assert refreshed.get("device_count") == 1, (
            f"group must have exactly one member for this test, got: {refreshed!r}"
        )

        # All-day schedule: guaranteed to be in-window regardless of CMS tz.
        schedule = _api_post(page, "/api/schedules", {
            "name": SCHEDULE_NAME,
            "group_id": group_id,
            "asset_id": asset["id"],
            "start_time": "00:00:00",
            "end_time": "23:59:00",
            "priority": 0,
            "enabled": True,
        })
        schedule_id = schedule["id"]

        # Wait for actual playback to start: the device sends PLAYBACK_STARTED
        # over WS, the dashboard's now_playing surfaces it.  This is the
        # strongest pre-condition we can assert without scraping the DOM.
        def _is_playing():
            d = _api_get(page, "/api/dashboard")
            for np in d.get("now_playing", []):
                if (np.get("schedule_name") == SCHEDULE_NAME
                        and np.get("device_id") == target_serial):
                    return d
            return None

        _poll_until(_is_playing, desc=f"{SCHEDULE_NAME} playing on {target_serial}")

        # End Now scoped to this device.  Asserting the response echo guards
        # against a JSON parse failure silently routing into the legacy
        # schedule-wide path (which would also clear upcoming, masking the
        # real regression behaviour).
        end_resp = _api_post_json(
            page,
            f"/api/schedules/{schedule_id}/end-now",
            {"device_id": target_serial},
        )
        assert end_resp.get("device_id") == target_serial, (
            f"end-now did not take per-device path: {end_resp!r}"
        )
        assert end_resp.get("ended") == schedule_id

        # The regression assertion: schedule_name must vanish from BOTH
        # now_playing (for this device) AND upcoming (no "starting" badge).
        # Pre-fix: now_playing cleared but a starting:true entry reappeared
        # in upcoming within seconds.
        def _cleared_everywhere():
            d = _api_get(page, "/api/dashboard")
            still_playing = any(
                np.get("schedule_name") == SCHEDULE_NAME
                and np.get("device_id") == target_serial
                for np in d.get("now_playing", [])
            )
            still_upcoming = any(
                e.get("schedule_name") == SCHEDULE_NAME
                for e in d.get("upcoming", [])
            )
            return d if (not still_playing and not still_upcoming) else None

        final = _poll_until(
            _cleared_everywhere,
            desc=f"{SCHEDULE_NAME} cleared from now_playing AND upcoming",
        )
        # Belt-and-suspenders: explicitly assert no entry is "starting"
        # for this schedule — that was the exact symptom of #415.
        starting = [
            e for e in final.get("upcoming", [])
            if e.get("schedule_name") == SCHEDULE_NAME and e.get("starting")
        ]
        assert not starting, (
            f"schedule still has a 'starting' entry in upcoming "
            f"after per-device End Now: {starting!r}"
        )

    finally:
        if schedule_id:
            _api_delete(page, f"/api/schedules/{schedule_id}")
        if group_id:
            # Move device back to its original group (may be None).
            _api_patch(
                page, f"/api/devices/{target_serial}",
                {"group_id": orig_group_id},
            )
            _api_delete(page, f"/api/devices/groups/{group_id}")
