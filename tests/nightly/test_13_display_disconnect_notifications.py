"""Phase 13: display-disconnected -> scoped notification round-trip.

End-to-end coverage of the alert_service display-monitoring path:

    sim display fault -> cms_client WS heartbeat -> alert_service grace
        -> Notification (scope=group) + DeviceEvent
            (display_disconnected | display_connected)
        -> dashboard recent activity / notification bell / event log

Mirrors the thermal smoke test (Phase 9 / test_08) but for the display
state pipeline added in agora-cms (display badges + bell notifications)
and the agora-device-simulator ``display_connected`` fault knob.

Round-trip phases exercised in order:

  01 - admin pins an adopted device to Group A.
  02 - simulator sets ``display_connected=false`` via the fault API and
       the CMS observes it within a few heartbeats.
  03 - admin event log + dashboard "Recent Activity" pick up the
       DISPLAY_DISCONNECTED row immediately (no grace period applies to
       the event log, only to the bell notification).
  04 - after the production grace period (~120 s) alert_service emits a
       Notification (scope=group, level=warning, event=display_disconnected)
       which admin and Operator A (Group A) both see; Operator B
       (Group B) does NOT.
  05 - reconnect: simulator clears the fault. ws.py writes the
       DISPLAY_CONNECTED DeviceEvent and alert_service emits a
       follow-up DISPLAY_CONNECTED notification (level=info).
  99 - belt-and-braces cleanup: clear the simulator fault.

Depends on Phases 0-7 having set up OOBE, adopted devices, and created
the RBAC fixtures (Operator A / Viewer in Group A, Operator B in Group B).
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from playwright.sync_api import BrowserContext, Page

from tests.nightly.test_06_rbac import (
    OPERATOR_A,
    OPERATOR_B,
    STATE as RBAC_STATE,
    VIEWER,
    _login_page,
)


# State propagation: heartbeat-driven, ~3 s rapid cadence on transition.
DISPLAY_PROPAGATION_TIMEOUT_S = 60.0
# Production grace (alert_service.DEFAULT_DISPLAY_GRACE_SECONDS = 120). We
# poll for the notification past this window with a generous margin so a
# slow CI runner doesn't flake the test.
DISPLAY_GRACE_SECONDS = 120
NOTIF_TIMEOUT_S = DISPLAY_GRACE_SECONDS + 60.0
# DeviceEvent emission is fire-and-forget after the WS message is processed.
EVENT_EMISSION_TIMEOUT_S = 15.0


# Shared state populated by earlier tests in this module.
DISPLAY_STATE: dict[str, Any] = {
    "device_id": None,      # CMS UUID of the device we pinned to Group A
    "device_serial": None,  # simulator serial used to trigger the fault
}


# ── helpers ───────────────────────────────────────────────────────────────


def _list_notifications(page: Page) -> list[dict]:
    resp = page.request.get("/api/notifications?limit=200")
    assert resp.status == 200, f"list notifications -> {resp.status}: {resp.text()[:400]}"
    return resp.json()


def _list_device_events(
    page: Page, *, device_id: str | None = None, event_type: str | None = None
) -> list[dict]:
    qs = []
    if device_id:
        qs.append(f"device_id={device_id}")
    if event_type:
        qs.append(f"event_type={event_type}")
    qs.append("limit=200")
    url = "/api/device-events?" + "&".join(qs)
    resp = page.request.get(url)
    assert resp.status == 200, f"list device-events -> {resp.status}: {resp.text()[:400]}"
    return resp.json()


def _notifs_for(notifs: list[dict], device_id: str, event_type: str) -> list[dict]:
    """Filter notifications to ones emitted by alert_service for (device, event_type)."""
    out: list[dict] = []
    for n in notifs:
        det = n.get("details") or {}
        if str(det.get("device_id", "")) != str(device_id):
            continue
        if str(det.get("event_type", "")) != event_type:
            continue
        out.append(n)
    return out


def _has_notif_for(notifs: list[dict], device_id: str, event_type: str) -> bool:
    return bool(_notifs_for(notifs, device_id, event_type))


def _wait_for_event(
    page: Page, device_id: str, event_type: str, *, timeout: float = EVENT_EMISSION_TIMEOUT_S
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = _list_device_events(page, device_id=device_id, event_type=event_type)
        if events:
            return events[0]
        time.sleep(0.5)
    pytest.fail(
        f"no {event_type!r} device event for device {device_id} after {timeout}s"
    )


def _wait_for_notif(
    page: Page, device_id: str, event_type: str, *, timeout: float
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = _notifs_for(_list_notifications(page), device_id, event_type)
        if matches:
            return matches[0]
        # Long-sleep loop — poll less often to keep server load low.
        time.sleep(2.0)
    pytest.fail(
        f"no {event_type!r} notification for device {device_id} after {timeout}s"
    )


def _wait_for_display_state(
    page: Page, device_id: str, *, expected: bool
) -> bool:
    """Poll GET /api/devices/{id} until display_connected matches the expected bool."""
    deadline = time.monotonic() + DISPLAY_PROPAGATION_TIMEOUT_S
    last: Any = "<unread>"
    while time.monotonic() < deadline:
        resp = page.request.get(f"/api/devices/{device_id}")
        if resp.status == 200:
            last = resp.json().get("display_connected")
            if last is expected:
                return last
        time.sleep(0.5)
    pytest.fail(
        f"display_connected never reached {expected!r} for device {device_id} "
        f"after {DISPLAY_PROPAGATION_TIMEOUT_S}s; last value = {last!r}"
    )


# ── tests ─────────────────────────────────────────────────────────────────


def test_01_pin_device_to_group_a(authenticated_page: Page) -> None:
    """Pin an adopted device into Group A so we can assert per-group RBAC later."""
    group_a = RBAC_STATE.get("group_a_id")
    assert group_a, "Phase 7 must have created Group A"

    resp = authenticated_page.request.get("/api/devices")
    assert resp.status == 200
    devices = resp.json()
    adopted = [d for d in devices if d.get("status") in (None, "adopted", "active")]
    assert adopted, "expected at least one adopted device from Phase 3"
    device = adopted[0]
    device_id = device["id"]

    patch = authenticated_page.request.patch(
        f"/api/devices/{device_id}", data={"group_id": group_a}
    )
    assert patch.status == 200, (
        f"pin device -> group A failed: {patch.status} {patch.text()[:400]}"
    )
    assert str(patch.json().get("group_id")) == str(group_a)

    DISPLAY_STATE["device_id"] = device_id
    # DeviceOut.id IS the serial for adopted Pis (see test_08 comment).
    DISPLAY_STATE["device_serial"] = device_id


def test_02_simulator_disconnects_display(simulator, authenticated_page: Page) -> None:
    """Drive ``display_connected=false`` via the sim fault API; CMS observes it."""
    device_id = DISPLAY_STATE["device_id"]
    serial = DISPLAY_STATE["device_serial"]
    assert device_id and serial

    simulator.apply_fault(serial, display_connected=False)
    try:
        observed = _wait_for_display_state(
            authenticated_page, device_id, expected=False
        )
        assert observed is False, f"expected display_connected=False; saw {observed!r}"
    except Exception:
        # Don't leave the device in a faulted state if we abort here.
        try:
            simulator.apply_fault(serial, display_connected=True)
        except Exception:
            pass
        raise
    # Leave the fault applied; later phases assert event log + notification
    # round-trip while the device is still in the disconnected state.


def test_03_event_log_and_recent_activity_show_disconnect(
    authenticated_page: Page,
) -> None:
    """The event log + dashboard 'Recent Activity' pick up DISPLAY_DISCONNECTED.

    Note: the bell notification is gated on a grace period (Phase 04).
    The event log row is written immediately by ws.py on every transition,
    independent of any grace timer.
    """
    device_id = DISPLAY_STATE["device_id"]
    assert device_id

    event = _wait_for_event(authenticated_page, device_id, "display_disconnected")
    assert event["event_type"] == "display_disconnected"
    assert str(event["device_id"]) == str(device_id)

    # Dashboard "Recent Activity" panel renders this event class. Friendly
    # labels were added when display badges shipped — accept either spelling
    # so a future copy tweak doesn't flake the smoke test.
    resp = authenticated_page.request.get("/")
    assert resp.status == 200
    html = resp.text().lower()
    assert (
        "display off" in html
        or "display disconnected" in html
        or "display_disconnected" in html
    ), "dashboard recent activity does not reference the display-disconnect event"


def test_04_notification_fires_after_grace_period(
    authenticated_page: Page, browser_context: BrowserContext
) -> None:
    """After the grace window, alert_service emits a scoped notification.

    Admin + Operator A (member of Group A) both see it. Viewer (also Group A,
    read-only) sees it too. Operator B (Group B) does NOT — negative RBAC.

    This phase deliberately waits the production grace period (~2 minutes)
    so the test reflects what users actually experience.
    """
    device_id = DISPLAY_STATE["device_id"]
    assert device_id

    notif = _wait_for_notif(
        authenticated_page, device_id, "display_disconnected",
        timeout=NOTIF_TIMEOUT_S,
    )
    assert notif["scope"] == "group", notif
    assert str(notif["group_id"]) == str(RBAC_STATE["group_a_id"]), notif
    assert notif["level"] == "warning", (
        f"display-disconnect notif must be warning, got {notif['level']}"
    )

    # Operator A (same group) sees it.
    op_a_page = _login_page(
        browser_context, OPERATOR_A["email"], OPERATOR_A["password"]
    )
    assert _has_notif_for(
        _list_notifications(op_a_page), device_id, "display_disconnected"
    ), "Operator A (Group A) should see the display-disconnect notification"

    # Viewer (same group, read-only) sees it too.
    v_page = _login_page(browser_context, VIEWER["email"], VIEWER["password"])
    assert _has_notif_for(
        _list_notifications(v_page), device_id, "display_disconnected"
    ), "Viewer (Group A) should see the display-disconnect notification"

    # NEGATIVE: Operator B (Group B) must NOT see it.
    op_b_page = _login_page(
        browser_context, OPERATOR_B["email"], OPERATOR_B["password"]
    )
    assert not _has_notif_for(
        _list_notifications(op_b_page), device_id, "display_disconnected"
    ), "Operator B (Group B) must not see Group A's display notification"

    # And no display_disconnected event row in their event log either.
    op_b_events = _list_device_events(
        op_b_page, device_id=device_id, event_type="display_disconnected"
    )
    assert op_b_events == [], (
        f"Operator B must not see DISPLAY_DISCONNECTED for a Group A device; "
        f"got {op_b_events!r}"
    )


def test_05_reconnect_emits_recovery_notification_and_event(
    simulator, authenticated_page: Page
) -> None:
    """Plug the display back in. CMS emits display_connected event + notif."""
    device_id = DISPLAY_STATE["device_id"]
    serial = DISPLAY_STATE["device_serial"]
    assert device_id and serial

    simulator.apply_fault(serial, display_connected=True)

    observed = _wait_for_display_state(
        authenticated_page, device_id, expected=True
    )
    assert observed is True

    # Recovery DeviceEvent appears in the event log immediately.
    event = _wait_for_event(authenticated_page, device_id, "display_connected")
    assert event["event_type"] == "display_connected"
    assert str(event["device_id"]) == str(device_id)

    # alert_service follows up the disconnect alert with an info-level
    # reconnect notification (no grace period — this fires immediately).
    recovered = _wait_for_notif(
        authenticated_page, device_id, "display_connected",
        timeout=EVENT_EMISSION_TIMEOUT_S,
    )
    assert recovered["level"] == "info", recovered
    assert recovered["scope"] == "group"
    assert str(recovered["group_id"]) == str(RBAC_STATE["group_a_id"])


def test_99_cleanup(simulator) -> None:
    """Clear the simulator fault so the device is back in its baseline state."""
    serial = DISPLAY_STATE.get("device_serial")
    if not serial:
        return
    try:
        simulator.clear_faults(serial)
    except Exception:
        pass
