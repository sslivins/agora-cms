"""Phase 9: thermal threshold -> scoped notification (#250, #263).

Pins the spec from issue #263: when a device's ``cpu_temp_c`` crosses the
``>=80 C`` critical threshold the CMS should emit a persistent
``Notification`` whose visibility tracks the device's group_id so that

- admin (has ``groups:view_all``)        -> sees it
- operator in the same group              -> sees it
- viewer in the same group                -> sees it (read-only)
- operator in a DIFFERENT group           -> does NOT see it

Today the product has the Notification model + scope-based visibility
filter in place, but nothing in ``cms/`` emits a group-scoped notification
for a telemetry event. The telemetry -> notification path is tracked in
#263.

Strategy:

- Active assertions (should pass today):
    1. Simulator ``apply_fault(cpu_temp=85)`` makes it through to the CMS.
    2. ``GET /api/devices/{id}`` reflects ``cpu_temp_c >= 80``.
    3. The dashboard HTML renders the critical row for that device.

- ``xfail`` assertions (pinned spec for #263 -- flip to active when feature
  lands):
    4. A new ``Notification`` row exists with scope=group, group_id=<device group>.
    5. Admin sees the notification in their list.
    6. Operator in the device's group sees it.
    7. Viewer in the device's group sees it.
    8. Operator in a different group does NOT see it.

Depends on Phases 0-7 having set up OOBE, adopted devices, and created the
RBAC fixtures (Operator A / Viewer in Group A, Operator B in Group B).
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from playwright.sync_api import BrowserContext, Page

from tests.nightly.test_06_rbac import (
    OPERATOR_A,
    OPERATOR_B,
    STATE as RBAC_STATE,
    VIEWER,
    _login_page,
)


CRITICAL_TEMP_C = 85.0
TEMP_PROPAGATION_TIMEOUT_S = 30.0


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
    """Force the device to 85 C via the sim fault API and confirm CMS sees it."""
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
    # Leave the fault applied for the later tests in this module; final
    # cleanup test clears it.


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


# ── #263 spec (xfail until the feature lands) ─────────────────────────────


@pytest.mark.xfail(
    reason="#263: no code path in CMS emits a group-scoped notification "
    "for cpu_temp_c>=80 yet. Flip xfail -> active when feature lands.",
    strict=True,
)
def test_04_admin_receives_thermal_notification(authenticated_page: Page) -> None:
    """Admin's notification feed should contain the critical thermal event."""
    device_id = THERMAL_STATE["device_id"]
    assert device_id

    # Give the (future) emitter a moment to land the notification.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        notifs = _list_notifications(authenticated_page)
        if _has_thermal_notif_for(notifs, device_id):
            # Also assert the scope + group_id shape per #263 AC-1.
            match = next(
                n for n in notifs
                if str((n.get("details") or {}).get("device_id", "")) == str(device_id)
                or str(device_id).lower() in f"{n.get('title','')} {n.get('message','')}".lower()
            )
            assert match.get("scope") == "group"
            assert str(match.get("group_id")) == str(RBAC_STATE["group_a_id"])
            assert match.get("level") == "error"
            return
        time.sleep(0.5)
    pytest.fail(f"admin never saw a thermal notification for device {device_id}")


@pytest.mark.xfail(reason="#263: depends on group-scoped thermal notification path.", strict=True)
def test_05_operator_in_same_group_sees_notification(browser_context: BrowserContext) -> None:
    device_id = THERMAL_STATE["device_id"]
    op_page = _login_page(browser_context, OPERATOR_A["email"], OPERATOR_A["password"])
    assert _has_thermal_notif_for(_list_notifications(op_page), device_id), (
        "Operator A (Group A) should see the thermal notification"
    )


@pytest.mark.xfail(reason="#263: depends on group-scoped thermal notification path.", strict=True)
def test_06_viewer_in_same_group_sees_notification(browser_context: BrowserContext) -> None:
    device_id = THERMAL_STATE["device_id"]
    v_page = _login_page(browser_context, VIEWER["email"], VIEWER["password"])
    assert _has_thermal_notif_for(_list_notifications(v_page), device_id), (
        "Viewer (Group A) should see the thermal notification (read-only)"
    )


@pytest.mark.xfail(reason="#263: depends on group-scoped thermal notification path.", strict=True)
def test_07_operator_in_other_group_does_not_see_notification(
    browser_context: BrowserContext,
) -> None:
    """Cross-group isolation: Operator B (Group B) must NOT see Group A's thermal event.

    xfail is strict + inverted: this will report as xfail as long as the
    notification doesn't exist at all. Once #263 lands and the feature is
    correct, Operator B's list will still not contain the notification, so
    this test must be rewritten when xfail is flipped:
        ``assert not _has_thermal_notif_for(...)``
    """
    device_id = THERMAL_STATE["device_id"]
    op_b_page = _login_page(browser_context, OPERATOR_B["email"], OPERATOR_B["password"])
    # Intentionally asserts PRESENCE so it currently xfails. When #263 is
    # implemented, swap to ``assert not _has_thermal_notif_for(...)``.
    assert _has_thermal_notif_for(_list_notifications(op_b_page), device_id)


# ── cleanup ───────────────────────────────────────────────────────────────


def test_99_clear_simulator_fault(simulator) -> None:
    """Restore the device's temperature so later phases / reruns start clean."""
    serial = THERMAL_STATE.get("device_serial")
    if serial:
        simulator.clear_faults(serial)
