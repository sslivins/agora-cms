"""Phase 9: thermal threshold -> scoped notification round-trip (#250, #263).

End-to-end coverage of the alert_service thermal-monitoring path:

    sim fault -> cms_client WS heartbeat -> alert_service state machine
        -> Notification (scope=group) + DeviceEvent (temp_high|temp_cleared)
        -> dashboard banner / notification bell / event log

Three-user RBAC matrix (admin, operator-in-group, operator-in-other-group):

- admin (``groups:view_all``)                  -> sees everything everywhere
- operator in the device's group               -> sees dashboard + notif + event
- viewer in the device's group                 -> sees notif (read-only)
- operator in a DIFFERENT group (negative)     -> sees NONE of it

Round-trip phases exercised in order:

  01 - admin pins an adopted device to Group A
  02 - simulator forces cpu_temp to 82 C (critical, >= DEFAULT_TEMP_CRITICAL_C)
  03 - admin dashboard shows badge-temp-critical for the device
  04 - admin sees thermal Notification + /api/notifications/count >= 1 +
       TEMP_HIGH row on the event log
  05 - Operator A (Group A) sees the notification + event log row
  06 - Viewer (Group A, read-only) sees the notification
  07 - NEGATIVE: Operator B (Group B) sees no thermal notif for this device,
       no TEMP_HIGH row in their event log, and no dashboard banner either
  08 - clear phase: simulator drops cpu_temp to 50 C (sub-warning).
       alert_service emits a TEMP_CLEARED Notification (level=info) +
       DeviceEvent(temp_cleared). Admin + Operator A see the cleared state.
       Dashboard no longer renders badge-temp-critical for the device.
  99 - belt-and-braces cleanup: clear all simulator faults

Depends on Phases 0-7 having set up OOBE, adopted devices, and created the
RBAC fixtures (Operator A / Viewer in Group A, Operator B in Group B).

Default alert thresholds (``cms.services.alert_service``):
  WARNING  = 70.0 C   ->  Notification level="warning"
  CRITICAL = 80.0 C   ->  Notification level="error"
  CLEAR    =  <WARNING ->  Notification level="info", event TEMP_CLEARED
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


CRITICAL_TEMP_C = 82.0
SUB_WARNING_TEMP_C = 50.0
# Simulator heartbeat is 30 s (STATUS_INTERVAL in agora.cms_client.service);
# state changes kick in a rapid 3 s cadence. Give up to 60 s to see a value
# land in the CMS.
TEMP_PROPAGATION_TIMEOUT_S = 60.0
# Once the CMS observes the threshold crossing, the notification + event
# are created in a fire-and-forget asyncio task. Poll briefly for them.
ALERT_EMISSION_TIMEOUT_S = 15.0


# Shared state populated by earlier tests in this module.
THERMAL_STATE: dict[str, Any] = {
    "device_id": None,      # CMS UUID of the device we pinned to Group A
    "device_serial": None,  # simulator serial used to trigger the fault
}


# ── helpers ───────────────────────────────────────────────────────────────


def _wait_for_temp(page: Page, device_id: str, *, at_least: float) -> float:
    """Poll GET /api/devices/{id} until cpu_temp_c >= at_least or timeout."""
    deadline = time.monotonic() + TEMP_PROPAGATION_TIMEOUT_S
    last_val: float | None = None
    while time.monotonic() < deadline:
        resp = page.request.get(f"/api/devices/{device_id}")
        if resp.status == 200:
            body = resp.json()
            last_val = body.get("cpu_temp_c")
            if last_val is not None and last_val >= at_least:
                return last_val
        time.sleep(0.5)
    pytest.fail(
        f"cpu_temp_c never reached >={at_least} for device {device_id} "
        f"after {TEMP_PROPAGATION_TIMEOUT_S}s; last value = {last_val!r}"
    )


def _list_notifications(page: Page) -> list[dict]:
    resp = page.request.get("/api/notifications?limit=200")
    assert resp.status == 200, f"list notifications -> {resp.status}: {resp.text()[:400]}"
    return resp.json()


def _notification_count(page: Page) -> int:
    resp = page.request.get("/api/notifications/count")
    assert resp.status == 200, f"notif count -> {resp.status}: {resp.text()[:200]}"
    return int(resp.json().get("unread", 0))


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


def _thermal_notifs_for(notifs: list[dict], device_id: str, event_type: str) -> list[dict]:
    """Filter `notifs` to ones emitted by alert_service for this device + event.

    alert_service stores ``details={"device_id": ..., "event_type": ..., ...}``.
    """
    out: list[dict] = []
    for n in notifs:
        det = n.get("details") or {}
        if str(det.get("device_id", "")) != str(device_id):
            continue
        if str(det.get("event_type", "")) != event_type:
            continue
        out.append(n)
    return out


def _has_thermal_notif_for(notifs: list[dict], device_id: str) -> bool:
    """True if any notif in the list refers to our device hitting the thermal threshold."""
    for n in notifs:
        det = n.get("details") or {}
        if str(det.get("device_id", "")) == str(device_id):
            return True
        # Fallback: title/message often name the device -- check device_id appears anywhere.
        blob = f"{n.get('title','')} {n.get('message','')}".lower()
        if str(device_id).lower() in blob:
            return True
    return False


def _wait_for_thermal_notif(
    page: Page, device_id: str, event_type: str, *, timeout: float = ALERT_EMISSION_TIMEOUT_S
) -> dict:
    """Poll admin's notification feed until a notif for (device, event_type) appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = _thermal_notifs_for(_list_notifications(page), device_id, event_type)
        if matches:
            return matches[0]
        time.sleep(0.5)
    pytest.fail(
        f"no {event_type!r} notification for device {device_id} after {timeout}s"
    )


def _wait_for_thermal_event(
    page: Page, device_id: str, event_type: str, *, timeout: float = ALERT_EMISSION_TIMEOUT_S
) -> dict:
    """Poll the device-events endpoint until a matching row appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = _list_device_events(page, device_id=device_id, event_type=event_type)
        if events:
            return events[0]
        time.sleep(0.5)
    pytest.fail(
        f"no {event_type!r} device event for device {device_id} after {timeout}s"
    )


def _wait_for_temp_below(page: Page, device_id: str, *, below: float) -> float:
    """Poll GET /api/devices/{id} until cpu_temp_c < below or timeout."""
    deadline = time.monotonic() + TEMP_PROPAGATION_TIMEOUT_S
    last_val: float | None = None
    while time.monotonic() < deadline:
        resp = page.request.get(f"/api/devices/{device_id}")
        if resp.status == 200:
            body = resp.json()
            last_val = body.get("cpu_temp_c")
            if last_val is not None and last_val < below:
                return last_val
        time.sleep(0.5)
    pytest.fail(
        f"cpu_temp_c never dropped below {below} for device {device_id} "
        f"after {TEMP_PROPAGATION_TIMEOUT_S}s; last value = {last_val!r}"
    )


# ── tests ─────────────────────────────────────────────────────────────────


def test_01_assign_device_to_group_a(authenticated_page: Page) -> None:
    """Admin picks an adopted device and pins it to Group A (from Phase 7)."""
    group_a = RBAC_STATE.get("group_a_id")
    assert group_a, "Phase 7 must have created Group A"

    resp = authenticated_page.request.get("/api/devices")
    assert resp.status == 200
    devices = resp.json()
    # Pick the first adopted device and move it into Group A.
    adopted = [d for d in devices if d.get("status") in (None, "adopted", "active")]
    assert adopted, "expected at least one adopted device from Phase 3"
    device = adopted[0]
    device_id = device["id"]

    patch = authenticated_page.request.patch(
        f"/api/devices/{device_id}",
        data={"group_id": group_a},
    )
    assert patch.status == 200, f"pin device -> group A failed: {patch.status} {patch.text()[:400]}"
    body = patch.json()
    assert str(body.get("group_id")) == str(group_a)

    THERMAL_STATE["device_id"] = device_id
    # DeviceOut.id IS the serial for adopted Pis (adoption uses the Pi's
    # serial as the primary key). That same value is the simulator's serial.
    THERMAL_STATE["device_serial"] = device_id


def test_02_simulator_forces_critical_temperature(
    simulator, authenticated_page: Page
) -> None:
    """Force the device to 82 C via the sim fault API and confirm CMS sees it."""
    device_id = THERMAL_STATE["device_id"]
    serial = THERMAL_STATE["device_serial"]
    assert device_id and serial

    simulator.apply_fault(serial, cpu_temp=CRITICAL_TEMP_C)
    try:
        observed = _wait_for_temp(authenticated_page, device_id, at_least=80.0)
        assert observed >= 80.0, f"temp did not cross 80 C; saw {observed}"
    except Exception:
        simulator.clear_faults(serial)
        raise
    # Leave the fault applied for the later tests in this module; the clear
    # phase (test_08) + final cleanup (test_99) restore normal state.


def test_03_dashboard_shows_critical_row_for_device(authenticated_page: Page) -> None:
    """The dashboard HTML renders a critical row for a device currently >=80 C."""
    device_id = THERMAL_STATE["device_id"]
    assert device_id

    resp = authenticated_page.request.get("/")
    assert resp.status == 200
    html = resp.text()
    assert "badge-temp-critical" in html, (
        "dashboard is not rendering the critical badge even though device is >=80 C; "
        "check cms/templates/dashboard.html temp_critical filter"
    )
    # Device identifier should appear in the rendered banner row. Be lenient --
    # dashboards render name, not id.
    assert ("Critical" in html) or ("critical" in html.lower()), html[:2000]


# ── #263 round-trip: admin + Operator A + Viewer see raise; Operator B doesn't ───


def test_04_admin_sees_thermal_notification_and_event(authenticated_page: Page) -> None:
    """Admin sees the raised Notification (scope=group, level=error) +
    /api/notifications/count >= 1 + a TEMP_HIGH row in the device event log.
    """
    device_id = THERMAL_STATE["device_id"]
    assert device_id

    # Notification content shape (per #263 AC-1: scope=group, tracks group_id)
    notif = _wait_for_thermal_notif(authenticated_page, device_id, "temp_high")
    assert notif["scope"] == "group", notif
    assert str(notif["group_id"]) == str(RBAC_STATE["group_a_id"]), notif
    assert notif["level"] == "error", f"82C must emit critical/error, got {notif['level']}"
    det = notif.get("details") or {}
    assert det.get("cpu_temp_c") is not None and det["cpu_temp_c"] >= 80.0, det

    # Notification bell unread count must reflect >= 1
    assert _notification_count(authenticated_page) >= 1, (
        "admin's unread notification count should include the thermal alert"
    )

    # Event log has a TEMP_HIGH row for this device with the temp in details.
    event = _wait_for_thermal_event(authenticated_page, device_id, "temp_high")
    assert event["event_type"] == "temp_high", event
    assert str(event["device_id"]) == str(device_id), event
    det = event.get("details") or {}
    assert det.get("cpu_temp_c") is not None and det["cpu_temp_c"] >= 80.0, det
    assert det.get("level") == "critical", det


def test_05_operator_in_same_group_sees_notification(
    browser_context: BrowserContext,
) -> None:
    """Operator A (member of Group A) sees the notif + event log row + dashboard critical."""
    device_id = THERMAL_STATE["device_id"]
    assert device_id

    op_page = _login_page(browser_context, OPERATOR_A["email"], OPERATOR_A["password"])

    # Notification via bell feed
    notifs = _thermal_notifs_for(
        _list_notifications(op_page), device_id, "temp_high"
    )
    assert notifs, "Operator A (Group A) should see the thermal notification"
    assert notifs[0]["level"] == "error"

    # Event log (RBAC: operator sees events for groups they belong to)
    events = _list_device_events(op_page, device_id=device_id, event_type="temp_high")
    assert events, "Operator A should see the TEMP_HIGH event for their group's device"

    # Dashboard renders the critical banner for them too
    resp = op_page.request.get("/")
    assert resp.status == 200
    html = resp.text()
    assert "badge-temp-critical" in html, (
        "Operator A dashboard should render badge-temp-critical for >=80C device"
    )


def test_06_viewer_in_same_group_sees_notification(
    browser_context: BrowserContext,
) -> None:
    """Viewer in Group A sees the notification (read-only)."""
    device_id = THERMAL_STATE["device_id"]
    assert device_id

    v_page = _login_page(browser_context, VIEWER["email"], VIEWER["password"])
    notifs = _thermal_notifs_for(
        _list_notifications(v_page), device_id, "temp_high"
    )
    assert notifs, "Viewer (Group A) should see the thermal notification (read-only)"


def test_07_operator_in_other_group_does_not_see_notification(
    browser_context: BrowserContext,
) -> None:
    """NEGATIVE: Operator B (Group B) must NOT see Group A's thermal event.

    Cross-group isolation check: notification feed, event log, and dashboard
    critical banner are all gated by group membership. Operator B belongs
    only to Group B; Group A's device being hot is none of their business.
    """
    device_id = THERMAL_STATE["device_id"]
    assert device_id

    op_b_page = _login_page(browser_context, OPERATOR_B["email"], OPERATOR_B["password"])

    # No thermal notification at all — not for this device, not for any device.
    assert not _has_thermal_notif_for(
        _list_notifications(op_b_page), device_id
    ), "Operator B (Group B) must not see Group A's thermal notification"

    # Event log for this device must be empty when queried by op_b.
    events = _list_device_events(op_b_page, device_id=device_id, event_type="temp_high")
    assert events == [], (
        f"Operator B must not see TEMP_HIGH events for a Group A device; got {events!r}"
    )

    # Dashboard: op_b has no devices >= 80C (their one device is the default
    # 45C). No critical badge should render for them.
    resp = op_b_page.request.get("/")
    assert resp.status == 200
    html = resp.text()
    assert "badge-temp-critical" not in html, (
        "Operator B dashboard must not show badge-temp-critical — they don't own "
        "the hot device and Group A is not in their visibility"
    )


# ── clear phase: drop below warning -> TEMP_CLEARED notification + event ───


def test_08_clear_fault_emits_cleared_notification(
    simulator, authenticated_page: Page, browser_context: BrowserContext
) -> None:
    """Drop cpu_temp below warning, assert TEMP_CLEARED emits + dashboard clears."""
    device_id = THERMAL_STATE["device_id"]
    serial = THERMAL_STATE["device_serial"]
    assert device_id and serial

    # Drive temp back into normal range (< WARNING=70C). Use an explicit value
    # rather than clear_faults() so we know exactly what the heartbeat reports.
    simulator.apply_fault(serial, cpu_temp=SUB_WARNING_TEMP_C)

    # Wait for the heartbeat carrying the new low temp to land.
    observed = _wait_for_temp_below(authenticated_page, device_id, below=70.0)
    assert observed < 70.0

    # alert_service emits the TEMP_CLEARED Notification (level=info) + event.
    cleared = _wait_for_thermal_notif(authenticated_page, device_id, "temp_cleared")
    assert cleared["level"] == "info", cleared
    assert cleared["scope"] == "group"
    assert str(cleared["group_id"]) == str(RBAC_STATE["group_a_id"])
    det = cleared.get("details") or {}
    assert det.get("cpu_temp_c") is not None and det["cpu_temp_c"] < 70.0

    # Event log has the TEMP_CLEARED row.
    event = _wait_for_thermal_event(authenticated_page, device_id, "temp_cleared")
    assert event["event_type"] == "temp_cleared"
    assert str(event["device_id"]) == str(device_id)

    # Operator A also sees the clearance (same group).
    op_page = _login_page(browser_context, OPERATOR_A["email"], OPERATOR_A["password"])
    op_cleared = _thermal_notifs_for(
        _list_notifications(op_page), device_id, "temp_cleared"
    )
    assert op_cleared, "Operator A should see the TEMP_CLEARED notification"
    op_events = _list_device_events(
        op_page, device_id=device_id, event_type="temp_cleared"
    )
    assert op_events, "Operator A should see the TEMP_CLEARED event log row"

    # Dashboard no longer renders badge-temp-critical (no devices >= 80C).
    # NB: dashboard gates the "issues" banner on ANY device being hot; since
    # all three test devices are now at or below profile default, the
    # critical/warning badges should be gone. The inner dashboard still
    # exists — we only assert the critical badge is absent.
    resp = authenticated_page.request.get("/")
    assert resp.status == 200
    html = resp.text()
    assert "badge-temp-critical" not in html, (
        "dashboard still rendering badge-temp-critical after temperature cleared"
    )


# ── cleanup ───────────────────────────────────────────────────────────────


def test_99_clear_simulator_fault(simulator) -> None:
    """Restore the device's temperature so later phases / reruns start clean."""
    serial = THERMAL_STATE.get("device_serial")
    if serial:
        simulator.clear_faults(serial)
