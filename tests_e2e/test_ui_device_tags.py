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


def _tag_box(page: Page, device_id: str):
    """The shared tag_chips() container for one device row."""
    return page.locator(
        f'.tag-chips[data-tag-scope="device"][data-tag-owner="{device_id}"]')


def _chips(page: Page, device_id: str):
    return _tag_box(page, device_id).locator(".tag-chip")


def _pick_tag(page: Page, device_id: str, tag_name: str):
    """Open the shared "+ tag" picker on a device row and choose a tag."""
    _tag_box(page, device_id).locator(".tag-add-btn").click()
    popup = page.locator("#tag-picker-popup")
    expect(popup).to_be_visible(timeout=3000)
    popup.get_by_role("button", name=tag_name).click()


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
        """Renaming writes on blur — there is no Save button to forget."""
        group_id = _make_group(api, "Rename Tag Group")
        _make_tag(api, group_id, "Draft Name")

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        modal = _open_tag_manager(page, group_id)

        row = modal.locator("[data-tag-row]").first
        expect(row.locator("[data-tag-save-row]")).to_have_count(0)

        row.locator("[data-tag-name]").fill("Final Name")
        row.locator("[data-tag-name]").blur()
        expect(page.locator(".toast-success")).to_be_visible(timeout=5000)

        tags = api.get(f"/api/groups/{group_id}/tags").json()
        assert [t["name"] for t in tags] == ["Final Name"]

    def test_backdrop_click_does_not_close_the_manager(self, page: Page, api, ws_url, e2e_server):
        """It holds live inputs, so a stray click outside must not dismiss it."""
        group_id = _make_group(api, "Sticky Modal Group")
        _make_tag(api, group_id, "Keeper")

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        modal = _open_tag_manager(page, group_id)

        page.locator(".modal-overlay").last.click(position={"x": 5, "y": 5})
        page.wait_for_timeout(300)
        expect(modal).to_be_visible()
        expect(modal.locator("[data-tag-row]")).to_have_count(1)

    def test_recolor_tag_saves_immediately(self, page: Page, api, ws_url, e2e_server):
        """The colour picker was the trap: picking a colour and closing the
        dialog used to discard it, because the write lived behind a Save."""
        group_id = _make_group(api, "Recolor Group")
        _make_tag(api, group_id, "Repaint", color="#3366cc")

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        modal = _open_tag_manager(page, group_id)

        row = modal.locator("[data-tag-row]").first
        row.locator("[data-tag-color]").fill("#ff8800")
        expect(page.locator(".toast-success")).to_be_visible(timeout=5000)

        # Close the dialog the lossy way — an outside click, not "Done".
        assert api.get(f"/api/groups/{group_id}/tags").json()[0]["color"] == "#ff8800"

    def test_rejected_rename_restores_the_field(self, page: Page, api, ws_url, e2e_server):
        """A refused write must not leave the input showing a value that
        isn't stored — the whole point of dropping the Save button."""
        group_id = _make_group(api, "Clash Group")
        _make_tag(api, group_id, "Taken")
        _make_tag(api, group_id, "Renameable")

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        modal = _open_tag_manager(page, group_id)

        # Rows are ordered by name, so "Renameable" is first.
        target = modal.locator("[data-tag-row]").first.locator("[data-tag-name]")
        expect(target).to_have_value("Renameable")
        target.fill("Taken")
        target.blur()

        expect(page.locator(".toast-error")).to_be_visible(timeout=5000)
        expect(target).to_have_value("Renameable")
        names = sorted(t["name"] for t in api.get(f"/api/groups/{group_id}/tags").json())
        assert names == ["Renameable", "Taken"]

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

        chips = _chips(page, "tag-dev-001")
        expect(chips).to_have_count(0)

        _pick_tag(page, "tag-dev-001", "Morning")

        expect(chips).to_have_count(1, timeout=5000)
        expect(chips.first).to_contain_text("Morning")

        assigned = api.get("/api/devices/tag-dev-001/tags").json()
        assert [t["name"] for t in assigned["tags"]] == ["Morning"]

    def test_chip_remove_button_untags_device(self, page: Page, api, ws_url, e2e_server):
        """The inline × on a chip is the only way to shed one tag."""
        group_id = _make_group(api, "Untag Group")
        _adopt(api, ws_url, "tag-dev-006", "Untag Device")
        api.patch("/api/devices/tag-dev-006", json={"group_id": group_id})
        tag_id = _make_tag(api, group_id, "Evening")
        api.put("/api/devices/tag-dev-006/tags", json={"tag_ids": [tag_id]})

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        expand_group_panel(page.locator(f'div.group-panel[data-group-id="{group_id}"]'))

        chips = _chips(page, "tag-dev-006")
        expect(chips).to_have_count(1)
        chips.first.locator(".tag-chip-remove").click()

        expect(chips).to_have_count(0, timeout=5000)
        assert api.get("/api/devices/tag-dev-006/tags").json()["tags"] == []

    def test_many_tags_collapse_behind_a_more_button(self, page: Page, api, ws_url, e2e_server):
        """A heavily tagged device must not blow out its table row."""
        group_id = _make_group(api, "Crowded Group")
        _adopt(api, ws_url, "tag-dev-007", "Crowded Device")
        api.patch("/api/devices/tag-dev-007", json={"group_id": group_id})
        tag_ids = [_make_tag(api, group_id, f"Tag {i}") for i in range(6)]
        api.put("/api/devices/tag-dev-007/tags", json={"tag_ids": tag_ids})

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        expand_group_panel(page.locator(f'div.group-panel[data-group-id="{group_id}"]'))

        box = _tag_box(page, "tag-dev-007")
        # All six are in the DOM -- the group-move warning reads tags from here.
        expect(_chips(page, "tag-dev-007")).to_have_count(6)
        # ...but only three are on screen, behind a "+3".
        expect(box.locator(".tag-chip:visible")).to_have_count(3)
        more = box.locator(".tag-chips-more")
        expect(more).to_have_text("+3")

        more.click()
        expect(box.locator(".tag-chip:visible")).to_have_count(6)
        expect(more).to_have_text("less")

        more.click()
        expect(box.locator(".tag-chip:visible")).to_have_count(3)

    def test_few_tags_render_no_more_button(self, page: Page, api, ws_url, e2e_server):
        group_id = _make_group(api, "Sparse Group")
        _adopt(api, ws_url, "tag-dev-008", "Sparse Device")
        api.patch("/api/devices/tag-dev-008", json={"group_id": group_id})
        api.put("/api/devices/tag-dev-008/tags", json={
            "tag_ids": [_make_tag(api, group_id, "Only One")]})

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        expand_group_panel(page.locator(f'div.group-panel[data-group-id="{group_id}"]'))

        expect(_tag_box(page, "tag-dev-008").locator(".tag-chips-more")).to_have_count(0)

    def test_long_tag_name_is_truncated_not_wrapped(self, page: Page, api, ws_url, e2e_server):
        """Names go to 64 chars; the chip clamps and keeps the full name in
        the tooltip rather than stretching the column."""
        group_id = _make_group(api, "Verbose Group")
        _adopt(api, ws_url, "tag-dev-009", "Verbose Device")
        api.patch("/api/devices/tag-dev-009", json={"group_id": group_id})
        long_name = "Seasonal Promotional Content For The Front Window Display"
        api.put("/api/devices/tag-dev-009/tags", json={
            "tag_ids": [_make_tag(api, group_id, long_name)]})

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        expand_group_panel(page.locator(f'div.group-panel[data-group-id="{group_id}"]'))

        label = _chips(page, "tag-dev-009").first.locator(".tag-chip-label")
        assert label.get_attribute("title").lower() == long_name.lower()
        width = label.evaluate("el => el.getBoundingClientRect().width")
        height = label.evaluate("el => el.getBoundingClientRect().height")
        # 9rem cap at the 16px root, plus a little slack for borders.
        assert width <= 150, f"chip label not clamped: {width}px"
        assert height < 30, f"chip label wrapped to a second line: {height}px"

    def test_editor_refuses_ungrouped_device(self, page: Page, api, ws_url, e2e_server):
        """A tag has no meaning without an owning group, so the picker declines."""
        _adopt(api, ws_url, "tag-dev-002", "Homeless Device")
        api.patch("/api/devices/tag-dev-002", json={"group_id": None})

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        _tag_box(page, "tag-dev-002").locator(".tag-add-btn").click()

        expect(page.locator(".toast-error")).to_be_visible(timeout=5000)
        expect(page.locator("#tag-picker-popup")).to_have_count(0)

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

        chips = _chips(page, "tag-dev-003")
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

        # One tag is fine; the second is the move the server must refuse.
        _pick_tag(page, "tag-dev-005", "Morning")
        expect(_chips(page, "tag-dev-005")).to_have_count(1, timeout=5000)

        _pick_tag(page, "tag-dev-005", "Promo")

        expect(page.locator(".toast-error")).to_be_visible(timeout=5000)
        # The rejected tag never lands, in the DOM or on the server.
        expect(_chips(page, "tag-dev-005")).to_have_count(1)
        assert [t["name"] for t in api.get(
            "/api/devices/tag-dev-005/tags").json()["tags"]] == ["Morning"]


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
