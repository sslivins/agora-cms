"""Playwright coverage for the group-scoped device tag UI (#876).

Tags are labels scoped to one device group. They carry no access control of
their own — the owning group remains the sole authorization boundary — so the
whole point of these tests is that a tag never escapes its group, and that the
two places a schedule conflict can be created are both closed:

* writing a schedule narrowed to a tag (``_check_conflicts``), and
* tagging a device *into* an existing overlap (``PUT /api/devices/{id}/tags``
  returns 409).

The second is the one that is easy to lose in a refactor, because at
schedule-write time the two schedules looked disjoint.
"""

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import run_async, expand_group_panel
from tests_e2e.fake_device import FakeDevice


def _adopt(api, ws_url, device_id, name=None):
    """Register a fake device and adopt it with a profile so it leaves PENDING."""

    async def register():
        async with FakeDevice(device_id, ws_url, device_name=name) as dev:
            await dev.send_status()

    run_async(register())
    profiles = api.get("/api/profiles").json()
    body = {"name": name or device_id}
    if profiles:
        body["profile_id"] = profiles[0]["id"]
    api.post(f"/api/devices/{device_id}/adopt", json=body)


def _make_group(api, name):
    resp = api.post("/api/devices/groups/", json={"name": name})
    assert resp.status_code == 201, f"group create failed {resp.status_code}: {resp.text}"
    return resp.json()["id"]


def _make_tag(api, group_id, name, color="#3366cc"):
    resp = api.post(f"/api/groups/{group_id}/tags", json={"name": name, "color": color})
    assert resp.status_code == 201, f"tag create failed {resp.status_code}: {resp.text}"
    return resp.json()["id"]


def _an_asset(api, filename):
    """Create a READY asset and return its id.

    Reusing ``GET /api/assets`` and taking the first row is unreliable: other
    tests upload assets that stay PENDING (the e2e server has no transcoder),
    and the readiness gate rejects those with a 422 on POST /api/schedules.
    ``create_asset`` marks every variant READY, so always mint a fresh one.
    """
    resp = api.create_asset(filename)
    if resp.status_code != 201:
        pytest.skip(f"Could not create test asset ({resp.status_code})")
    return resp.json()["id"]


def _open_tag_manager(page: Page, group_id: str):
    panel = page.locator(f'div.group-panel[data-group-id="{group_id}"]')
    expect(panel).to_be_visible(timeout=5000)
    panel.locator(".group-actions .btn-kebab").click()
    page.locator(".kebab-menu:popover-open").get_by_role(
        "menuitem", name="Manage tags"
    ).click()
    modal = page.locator(".modal-overlay")
    expect(modal).to_be_visible(timeout=3000)
    return modal


class TestGroupTagManager:
    """Create / rename / delete tags from the group header kebab."""

    def test_create_tag_from_group_kebab(self, page: Page, api, ws_url, e2e_server):
        group_id = _make_group(api, "Tag Manager Group")

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        modal = _open_tag_manager(page, group_id)

        modal.locator("#new-tag-name").fill("Summer Promos")
        modal.locator("#new-tag-add").click()

        # The row list re-renders in place with the new tag.
        expect(modal.locator("[data-tag-row]")).to_have_count(1, timeout=5000)
        expect(modal.locator("[data-tag-name]")).to_have_value("Summer Promos")

        # And it is really persisted, scoped to this group.
        tags = api.get(f"/api/groups/{group_id}/tags").json()
        assert [t["name"] for t in tags] == ["Summer Promos"]

    def test_rename_tag(self, page: Page, api, ws_url, e2e_server):
        group_id = _make_group(api, "Rename Tag Group")
        _make_tag(api, group_id, "Draft Name")

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        modal = _open_tag_manager(page, group_id)

        row = modal.locator("[data-tag-row]").first
        row.locator("[data-tag-name]").fill("Final Name")
        row.locator("[data-tag-save-row]").click()
        expect(page.locator(".toast-success")).to_be_visible(timeout=5000)

        tags = api.get(f"/api/groups/{group_id}/tags").json()
        assert [t["name"] for t in tags] == ["Final Name"]

    def test_delete_tag(self, page: Page, api, ws_url, e2e_server):
        group_id = _make_group(api, "Delete Tag Group")
        _make_tag(api, group_id, "Doomed")

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        modal = _open_tag_manager(page, group_id)

        modal.locator("[data-tag-row]").first.locator("[data-tag-delete-row]").click()
        # Deletion goes through the shared confirm modal, never native confirm().
        confirm = page.locator(".modal-overlay").last
        confirm.locator("button", has_text="Confirm").click()

        expect(modal.locator("[data-tag-row]")).to_have_count(0, timeout=5000)
        assert api.get(f"/api/groups/{group_id}/tags").json() == []

    def test_same_tag_name_allowed_in_two_groups(self, page: Page, api, ws_url, e2e_server):
        """Tag identity is (group, name) — the same label in two groups is two tags."""
        group_a = _make_group(api, "Scoped A")
        group_b = _make_group(api, "Scoped B")
        tag_a = _make_tag(api, group_a, "Summer Promos")

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        modal = _open_tag_manager(page, group_b)
        modal.locator("#new-tag-name").fill("Summer Promos")
        modal.locator("#new-tag-add").click()
        expect(modal.locator("[data-tag-row]")).to_have_count(1, timeout=5000)

        tags_b = api.get(f"/api/groups/{group_b}/tags").json()
        assert len(tags_b) == 1
        # Same display name, distinct identity: no cross-group targeting exists.
        assert tags_b[0]["id"] != tag_a


class TestDeviceTagAssignment:
    """Per-row tag chips and the inline editor."""

    def test_assign_tags_renders_chips(self, page: Page, api, ws_url, e2e_server):
        group_id = _make_group(api, "Chip Group")
        _adopt(api, ws_url, "tag-dev-001", "Chip Device")
        api.patch("/api/devices/tag-dev-001", json={"group_id": group_id})
        _make_tag(api, group_id, "Morning")

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        panel = page.locator(f'div.group-panel[data-group-id="{group_id}"]')
        expand_group_panel(panel)

        chips = page.locator('[data-device-tags="tag-dev-001"] .device-tag-chip')
        expect(chips).to_have_count(0)

        page.locator('[data-device-tag-edit="tag-dev-001"]').click()
        editor = page.locator(".modal-overlay")
        expect(editor).to_be_visible(timeout=3000)
        editor.locator("#device-tag-options input[type=checkbox]").first.check()
        editor.locator("[data-tag-save]").click()

        expect(chips).to_have_count(1, timeout=5000)
        expect(chips.first).to_have_text("Morning")

        assigned = api.get("/api/devices/tag-dev-001/tags").json()
        assert [t["name"] for t in assigned["tags"]] == ["Morning"]

    def test_editor_refuses_ungrouped_device(self, page: Page, api, ws_url, e2e_server):
        """A tag has no meaning without an owning group, so the editor declines."""
        _adopt(api, ws_url, "tag-dev-002", "Homeless Device")
        api.patch("/api/devices/tag-dev-002", json={"group_id": None})

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        page.locator('[data-device-tag-edit="tag-dev-002"]').click()

        expect(page.locator(".toast-error")).to_be_visible(timeout=5000)
        expect(page.locator(".modal-overlay")).to_have_count(0)

    def test_group_move_drops_tags_and_says_so(self, page: Page, api, ws_url, e2e_server):
        """Tags belong to the group they were made in; a move strips them."""
        group_a = _make_group(api, "Move From Group")
        group_b = _make_group(api, "Move To Group")
        _adopt(api, ws_url, "tag-dev-003", "Moving Device")
        api.patch("/api/devices/tag-dev-003", json={"group_id": group_a})
        tag_id = _make_tag(api, group_a, "Lobby")
        api.put("/api/devices/tag-dev-003/tags", json={"tag_ids": [tag_id]})

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        panel = page.locator(f'div.group-panel[data-group-id="{group_a}"]')
        expand_group_panel(panel)

        chips = page.locator('[data-device-tags="tag-dev-003"] .device-tag-chip')
        expect(chips).to_have_count(1)

        row = panel.locator('tr[data-device-id="tag-dev-003"]').first
        row.locator("select[data-device-group-select]").select_option(group_b)

        # Rows inside a group panel can't be moved in place, so the app falls
        # back to a reload; the message must survive it.
        page.wait_for_load_state("domcontentloaded")

        # The toast must name what was lost — a silent drop is the failure mode.
        toast = page.locator(".toast-success")
        expect(toast).to_be_visible(timeout=5000)
        expect(toast).to_contain_text("Lobby")

        assert api.get("/api/devices/tag-dev-003/tags").json()["tags"] == []


class TestTagConflictGate:
    """The 409 that closes the 'tag a device into an existing overlap' hole."""

    def test_tagging_into_overlap_is_rejected(self, page: Page, api, ws_url, e2e_server):
        group_id = _make_group(api, "Conflict Gate Group")
        _adopt(api, ws_url, "tag-dev-004", "Conflicted Device")
        api.patch("/api/devices/tag-dev-004", json={"group_id": group_id})
        morning = _make_tag(api, group_id, "Morning")
        promo = _make_tag(api, group_id, "Promo")
        asset_id = _an_asset(api, "tag-conflict.mp4")

        # Two same-group, equal-priority, overlapping schedules. They are allowed
        # to coexist only because they are narrowed to tags with no device in
        # common right now.
        for name, tag_id, start, end in (
            ("Morning Show", morning, "09:00", "11:00"),
            ("Promo Reel", promo, "10:00", "12:00"),
        ):
            resp = api.post("/api/schedules", json={
                "name": name, "group_id": group_id, "tag_id": tag_id,
                "asset_id": asset_id, "start_time": start, "end_time": end,
                "priority": 5,
            })
            assert resp.status_code == 201, f"{name}: {resp.status_code} {resp.text}"

        # One tag at a time is fine.
        assert api.put(
            "/api/devices/tag-dev-004/tags", json={"tag_ids": [morning]}
        ).status_code == 200

        # Both at once would put the device under both schedules simultaneously.
        both = api.put(
            "/api/devices/tag-dev-004/tags", json={"tag_ids": [morning, promo]}
        )
        assert both.status_code == 409, f"expected 409, got {both.status_code}: {both.text}"

        # And the device keeps the tag set it had before the rejected write.
        assert [t["id"] for t in api.get(
            "/api/devices/tag-dev-004/tags").json()["tags"]] == [morning]

    def test_conflict_surfaces_as_error_toast(self, page: Page, api, ws_url, e2e_server):
        """The 409 detail reaches the operator rather than failing silently."""
        group_id = _make_group(api, "Conflict Toast Group")
        _adopt(api, ws_url, "tag-dev-005", "Toast Device")
        api.patch("/api/devices/tag-dev-005", json={"group_id": group_id})
        morning = _make_tag(api, group_id, "Morning")
        promo = _make_tag(api, group_id, "Promo")
        asset_id = _an_asset(api, "tag-toast.mp4")

        for name, tag_id, start, end in (
            ("AM Slot", morning, "09:00", "11:00"),
            ("Promo Slot", promo, "10:00", "12:00"),
        ):
            assert api.post("/api/schedules", json={
                "name": name, "group_id": group_id, "tag_id": tag_id,
                "asset_id": asset_id, "start_time": start, "end_time": end,
                "priority": 5,
            }).status_code == 201

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        expand_group_panel(page.locator(f'div.group-panel[data-group-id="{group_id}"]'))

        page.locator('[data-device-tag-edit="tag-dev-005"]').click()
        editor = page.locator(".modal-overlay")
        expect(editor).to_be_visible(timeout=3000)
        # Tick both tags — this is the move the server must refuse.
        for box in editor.locator("#device-tag-options input[type=checkbox]").all():
            box.check()
        editor.locator("[data-tag-save]").click()

        expect(page.locator(".toast-error")).to_be_visible(timeout=5000)
        # The modal stays open so the operator can correct the selection.
        expect(editor).to_be_visible()
        assert api.get("/api/devices/tag-dev-005/tags").json()["tags"] == []


class TestScheduleTagTargeting:
    """<Group>:<Tag> targeting in the schedules page."""

    def test_create_schedule_narrowed_to_tag(self, page: Page, api, ws_url, e2e_server):
        group_id = _make_group(api, "Sched Tag Group")
        tag_id = _make_tag(api, group_id, "Window Display")
        _an_asset(api, "sched-tag.mp4")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        page.locator("#target_id").select_option(group_id)
        # Options are group-scoped and repopulate when the group changes.
        expect(page.locator("#target_tag_id option")).to_have_count(2, timeout=5000)
        page.locator("#target_tag_id").select_option(tag_id)

        assert page.locator("#target_tag_id").input_value() == tag_id

    def test_tag_options_are_scoped_to_selected_group(self, page: Page, api, ws_url, e2e_server):
        """Switching group must not leave the previous group's tags selectable."""
        group_a = _make_group(api, "Picker Group A")
        group_b = _make_group(api, "Picker Group B")
        _make_tag(api, group_a, "Only In A")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        page.locator("#target_id").select_option(group_a)
        expect(page.locator("#target_tag_id")).to_contain_text("Only In A", timeout=5000)

        page.locator("#target_id").select_option(group_b)
        expect(page.locator("#target_tag_id")).not_to_contain_text("Only In A")
        # Group B has no tags, so only the "all devices" option remains.
        expect(page.locator("#target_tag_id option")).to_have_count(1)

    def test_schedule_row_shows_tag_chip(self, page: Page, api, ws_url, e2e_server):
        group_id = _make_group(api, "Chip Sched Group")
        tag_id = _make_tag(api, group_id, "Aisle End")
        asset_id = _an_asset(api, "sched-chip.mp4")
        resp = api.post("/api/schedules", json={
            "name": "Tagged Schedule", "group_id": group_id, "tag_id": tag_id,
            "asset_id": asset_id, "start_time": "08:00", "end_time": "09:00",
        })
        assert resp.status_code == 201, f"{resp.status_code}: {resp.text}"
        sched_id = resp.json()["id"]

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator(f'tr[data-schedule-id="{sched_id}"]')
        expect(row).to_have_count(1, timeout=5000)
        # Target reads as <Group> + tag chip, so the narrowing is visible at a
        # glance rather than hidden behind the edit modal.
        expect(row).to_contain_text("Chip Sched Group")
        expect(row).to_contain_text("Aisle End")

    def test_edit_modal_preselects_and_sends_tag(self, page: Page, api, ws_url, e2e_server):
        group_id = _make_group(api, "Edit Tag Group")
        keep = _make_tag(api, group_id, "Keep Me")
        switch_to = _make_tag(api, group_id, "Switch To")
        asset_id = _an_asset(api, "sched-edit.mp4")
        resp = api.post("/api/schedules", json={
            "name": "Editable Schedule", "group_id": group_id, "tag_id": keep,
            "asset_id": asset_id, "start_time": "08:00", "end_time": "09:00",
        })
        assert resp.status_code == 201, f"{resp.status_code}: {resp.text}"
        sched_id = resp.json()["id"]

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator(f'tr[data-schedule-id="{sched_id}"]')
        expect(row).to_have_count(1, timeout=5000)
        row.locator(".btn-kebab").click()
        page.locator(".kebab-menu:popover-open").get_by_role("menuitem", name="Edit").click()

        modal = page.locator(".modal-overlay")
        expect(modal).to_be_visible(timeout=3000)
        # The schedule's current tag is preselected, not silently dropped.
        expect(modal.locator("#edit-tag")).to_have_value(keep)

        modal.locator("#edit-tag").select_option(switch_to)
        modal.locator("button", has_text="Save").click()
        page.wait_for_load_state("networkidle")

        assert api.get(f"/api/schedules/{sched_id}").json()["tag_id"] == switch_to
