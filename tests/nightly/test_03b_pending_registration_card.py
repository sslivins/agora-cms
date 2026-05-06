"""Phase 3.5: Pending Registrations card visible in /devices UI.

Regression coverage for the bug fixed in PR #535 + PR #536.

PR #478 was a half-done refactor: it deleted both the
``<section id="pending-card">`` markup AND the ``setInterval(pollPendingOnce,
5000)`` scheduler that drives it, while leaving the API endpoint and the
pollPendingOnce JS function in place as dead code.  None of the
unit-level pending-device tests caught it because they only asserted the
API contract or the rendered table rows for the *adopted* devices fed
through ``/ws/device``.

This nightly test exercises the **bootstrap registration** code path that
real Pis use during first boot:

1. POST /api/devices/register with valid fleet HMAC -> row in
   ``pending_registrations`` table.
2. /api/devices/pending returns the row.
3. /devices renders ``<section id="pending-card">``, the JS
   ``pollPendingOnce`` runs, the card un-hides, and the row is visible
   to admins as an adoptable pending device.

If any of those three steps regresses the test fails.

Requires the nightly stack (``--run-nightly``) because we need the
real Jinja-rendered HTML + the real JS poll loop running against a real
DB and a real running CMS container.
"""

from __future__ import annotations

import time

from playwright.sync_api import Page, expect

from tests.nightly.helpers import register_pending_device


PENDING_CARD_TIMEOUT_S = 15.0
# Card poller runs every 5s; give it two cycles + slack.

PENDING_CARD_SELECTOR = "#pending-devices-card"
PENDING_TBODY_SELECTOR = "#pending-devices-tbody"


def _delete_pending(page: Page, pending_id: str) -> None:
    """Best-effort cleanup so the card doesn't leak rows across tests."""
    try:
        page.request.delete(f"/api/devices/pending/{pending_id}")
    except Exception:  # noqa: BLE001
        pass


def _list_pending(page: Page) -> list[dict]:
    resp = page.request.get("/api/devices/pending")
    assert resp.status == 200, (
        f"GET /api/devices/pending -> {resp.status}: {resp.text()[:500]}"
    )
    return resp.json().get("items", [])


def test_pending_registration_shows_in_card(authenticated_page: Page) -> None:
    """Real /register -> /devices renders the device in #pending-devices-card."""
    page = authenticated_page

    registered = register_pending_device(page.request)

    # Confirm backend stored the row and grab the primary key for cleanup.
    # Fail fast with a clear error if the HMAC contract drifted.
    pending_rows = _list_pending(page)
    matches = [r for r in pending_rows if r["device_id"] == registered.device_id]
    assert matches, (
        f"registered device {registered.device_id!r} did not appear in "
        f"/api/devices/pending; got {len(pending_rows)} other rows"
    )
    pending_id = matches[0]["id"]

    try:
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        card = page.locator(PENDING_CARD_SELECTOR)
        # The card starts display:none until the JS poller fetches
        # /api/devices/pending and discovers a non-empty list.  This is
        # the exact contract PR #478 broke.
        expect(card).to_be_visible(timeout=int(PENDING_CARD_TIMEOUT_S * 1000))

        # The poller renders rows as
        #   <tr class="device-row" data-device-id="<device_id>">
        # inside #pending-devices-tbody, so a row tagged with our specific
        # device_id proves the card is surfacing this registration -- not
        # just some leftover row from a sibling test.
        device_row = page.locator(
            f'{PENDING_TBODY_SELECTOR} tr.device-row'
            f'[data-device-id="{registered.device_id}"]'
        )
        expect(device_row).to_be_visible(timeout=int(PENDING_CARD_TIMEOUT_S * 1000))

        # Adopt and Reject buttons must be present so an admin can act.
        expect(device_row.locator("button", has_text="Adopt")).to_have_count(1)
        expect(device_row.locator("button", has_text="Reject")).to_have_count(1)
    finally:
        _delete_pending(page, pending_id)


def test_pending_card_hides_when_list_empty(authenticated_page: Page) -> None:
    """With no pending registrations, the card stays display:none.

    Locks in the inverse contract: the poller MUST NOT leave the card
    visible when there's nothing to show.  Without this the card just
    looks like dead UI to admins.
    """
    page = authenticated_page

    # Drain any leftovers from prior tests so we have a true empty state.
    for r in _list_pending(page):
        _delete_pending(page, r["id"])

    page.goto("/devices")
    page.wait_for_load_state("domcontentloaded")

    # Give the poller a couple of cycles to do something silly.
    time.sleep(6)

    card = page.locator(PENDING_CARD_SELECTOR)
    # The element exists in the DOM (server-rendered) but should be
    # hidden -- the poller flips display based on payload length.
    expect(card).to_be_hidden()
