"""Regression test for the device-row kebab onclick HTML escaping.

The ``device_row`` macro in ``_macros.html`` builds each kebab menuitem
with an inline ``onclick="handler('{{ d.id }}', '{{ d.name or d.id }}')"``.
Jinja2's default autoescape rewrites ``'`` to ``&#39;`` inside the
attribute, but the browser's HTML parser then **decodes** ``&#39;``
back to ``'`` before handing the value to the JavaScript engine.

So a device named e.g. ``Mia's pi5`` produces::

    onclick="upgradeDevice('<id>', 'Mia&#39;s pi5')"

which the JS engine sees as::

    upgradeDevice('<id>', 'Mia's pi5')

That's a syntax error (``missing ) after argument list``) at element-
insertion time, so the onclick handler is never registered and clicking
the kebab item does nothing.

The fix is the same pattern the asset row's "Edit URL" handler uses:
filter through ``tojson | forceescape`` so the quoting becomes
``&#34;...&#34;`` (a JSON string literal whose own quote characters are
HTML-escaped). The browser decodes ``&#34;`` to ``"`` and the JS engine
gets a syntactically valid call.

This test seeds a device named ``Mia's pi5`` (the actual device that
first surfaced the bug) and asserts every kebab handler in the device
row renders the safe form.
"""

from __future__ import annotations

import re

import pytest
import pytest_asyncio


# The exact name reported by the user. The apostrophe is what breaks
# the broken pattern, so keep it verbatim.
DEVICE_NAME = "Mia's pi5"

# Every kebab handler in the device row that interpolates d.name.
# adoptDevice fires only on pending/orphaned status. The rest fire on
# adopted+online. The macro currently uses ``'{{ d.name or d.id }}'``
# for every one of them, so they all share the bug.
NAME_HANDLERS = (
    "upgradeDevice",
    "changeDevicePassword",
    "rebootDevice",
    "factoryResetDevice",
)


@pytest_asyncio.fixture
async def device_with_apostrophe(app):
    """Seed: one group + one adopted, online, update-available device whose
    name contains an apostrophe so every kebab onclick in the row is
    exercised."""
    from sqlalchemy import select

    from cms.database import get_db
    from cms.models.device import Device, DeviceGroup, DeviceStatus
    from cms.models.user import Role, User, UserGroup
    from cms.services import bundle_checker, device_presence

    factory = app.dependency_overrides[get_db]
    group_id = device_id = None
    async for db in factory():
        group = DeviceGroup(name="Apostrophe Group", description="")
        db.add(group)
        await db.flush()
        group_id = str(group.id)

        device = Device(
            id="apostrophe-device",
            name=DEVICE_NAME,
            status=DeviceStatus.ADOPTED,
            firmware_version="0.0.1",
            os_version="0.0.1",  # < latest, so update_available=True
            group_id=group.id,
        )
        db.add(device)
        await db.flush()

        # The kebab menu items that interpolate d.name (Update, Change
        # Web Password, Reboot, Factory Reset) are guarded on
        # ``d.is_online``, which is set from the transport's
        # connected_ids() in the panel handler. Mark the device online
        # in the presence table so the LocalDeviceTransport sees it.
        await device_presence.mark_online(db, device.id)

        # Grant any seeded non-admin user access so the panel endpoint
        # doesn't 403 in environments where the suite seeds extra users.
        admin_role_id = (
            await db.execute(select(Role.id).where(Role.name == "Admin"))
        ).scalar_one()
        non_admin_users = (
            await db.execute(select(User).where(User.role_id != admin_role_id))
        ).scalars().all()
        for u in non_admin_users:
            db.add(UserGroup(user_id=u.id, group_id=group.id))
        await db.commit()
        device_id = device.id
        break

    # Pin a latest bundle so update_available is decided server-side
    # (same scheme as grouped_update_device in test_devices_phase_d_coverage).
    async for db in factory():
        await bundle_checker.set_latest_bundle(
            db,
            bundle_checker.BundleInfo(
                target_version="9.9.9",
                release_id="stub",
                min_from_version="0.0.0",
                bundle_url="https://example.com/x.tar.zst",
                signature_url="https://example.com/x.tar.zst.minisig",
                sha256_url=None,
                size_bytes=0,
                created_at="2026-05-15T00:00:00Z",
            ),
        )
        await db.commit()
        break

    yield {"group_id": group_id, "device_id": device_id}


def _device_row_html(panel_html: str, device_id: str) -> str:
    """Extract the device-row <tr>...</tr> for a given id from a panel."""
    match = re.search(
        rf'(<tr [^>]*class="device-row[^"]*"[^>]*data-device-id="{re.escape(device_id)}"[^>]*>.*?</tr>)',
        panel_html,
        re.DOTALL,
    )
    assert match, f"device-row for {device_id} not found in panel HTML"
    return match.group(1)


@pytest.mark.asyncio
class TestDeviceKebabHtmlEscaping:

    async def test_kebab_onclick_does_not_break_on_apostrophe_name(
        self, client, device_with_apostrophe
    ):
        """Rendering a device with ``Mia's pi5`` as the name must not emit
        any onclick that the browser will decode into a syntactically broken
        JS expression. Specifically the broken form is::

            onclick="<handler>('<id>', 'Mia&#39;s pi5')"

        which the HTML parser decodes to ``<handler>('<id>', 'Mia's pi5')``
        — invalid JS.
        """
        gid = device_with_apostrophe["group_id"]
        did = device_with_apostrophe["device_id"]

        resp = await client.get(f"/api/devices/groups/{gid}/panel")
        assert resp.status_code == 200, resp.text

        row = _device_row_html(resp.text, did)

        for handler in NAME_HANDLERS:
            bad = f"{handler}('{did}', 'Mia&#39;s pi5')"
            assert bad not in row, (
                f"Broken HTML-attribute escaping for {handler} kebab onclick:\n"
                f"  raw apostrophe entity inside single-quoted JS literal will\n"
                f"  HTML-decode to a stray ' and break JS parsing.\n"
                f"  Use `{{{{ ... | tojson | forceescape }}}}` instead.\n"
                f"  Offending fragment: {bad!r}"
            )

    async def test_kebab_onclick_renders_html_safe_form(
        self, client, device_with_apostrophe
    ):
        """Positive assertion: every name-bearing kebab handler must render
        with the ``tojson | forceescape`` safe form, i.e. the URL/name is
        wrapped in HTML-encoded double quotes (``&#34;``) — the same pattern
        already used by ``editWebpageUrl`` / ``deleteProfile`` etc.
        """
        gid = device_with_apostrophe["group_id"]
        did = device_with_apostrophe["device_id"]

        resp = await client.get(f"/api/devices/groups/{gid}/panel")
        assert resp.status_code == 200, resp.text

        row = _device_row_html(resp.text, did)

        for handler in NAME_HANDLERS:
            # tojson | forceescape produces e.g.
            #     handler(&#34;<id>&#34;, &#34;Mia\u0027s pi5&#34;)
            # Both args should appear with the HTML-encoded double-quote.
            safe_call_start = f"{handler}(&#34;{did}&#34;"
            assert safe_call_start in row, (
                f"Expected {handler} to render with HTML-encoded JSON quotes "
                f"(&#34;...&#34;) around its arguments after applying "
                f"`| tojson | forceescape`. Got row HTML:\n{row}"
            )
