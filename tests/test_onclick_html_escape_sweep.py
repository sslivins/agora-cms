"""Regression sweep for the ``onclick="fn('{{ x }}')"`` HTML-escaping
bug pattern across all CMS templates.

The bug class
=============

Whenever a Jinja template renders an inline JS call inside a
double-quoted HTML attribute with user-typed text wrapped in single
quotes, like::

    onclick="handler('{{ user_typed_name }}')"

Jinja's autoescape rewrites a literal ``'`` in the value to ``&#39;``
inside the HTML attribute. The browser's HTML parser **decodes**
``&#39;`` back to ``'`` before handing the value to the JavaScript
engine. So a value like ``Mia's pi5`` ends up as the JS source::

    handler('Mia's pi5')

which is a syntax error — the handler is silently never registered
and clicking the button does nothing.

The fix is to filter both args through ``tojson | forceescape``
(matching the pattern used by ``editWebpageUrl`` from PR #589 and the
device-row kebab from PR #590). ``tojson`` produces a JSON string
literal quoted with ``"``; ``forceescape`` HTML-encodes those to
``&#34;``; the browser decodes ``&#34;`` to ``"`` inside the
attribute; the JS engine sees a valid double-quoted string. JSON's
``\u0027`` carries the apostrophe through cleanly.

What this file covers
=====================

Every template that had at least one offending onclick before this PR:

* ``_macros.html`` asset row — ``previewAsset``, ``recaptureStream``,
  ``deleteAsset``, ``pickAssetGroup``, ``previewVariant``
* ``assets.html`` upload/webpage/stream group dropdowns —
  ``pickUploadGroup``, ``pickWebpageGroup``, ``pickStreamGroup``
* ``slideshow_builder.html`` group dropdown — ``pickSsGroup``
* ``users.html`` row + role row — ``resendInvite``, ``deleteUser``,
  ``deleteRole``

The shape of each test is the same: seed an entity whose user-typed
field contains an apostrophe (``Mia's …``), hit the page or fragment
endpoint that renders it, and assert no onclick contains the broken
``'X&#39;Y'`` form. The negative assertion is page-wide because the
broken pattern is unambiguous — any single-quote-then-``&#39;``
sequence inside any onclick is a bug.
"""

from __future__ import annotations

import re

import pytest
import pytest_asyncio


# Find every onclick="..." attribute value and check each in isolation
# for the broken pattern: a single-quoted JS string literal whose
# contents include the HTML entity ``&#39;``.
_ONCLICK_RE = re.compile(r'onclick="([^"]*)"')
_BROKEN_QUOTED_LITERAL = re.compile(r"'[^']*&#39;[^']*'")


def assert_no_broken_onclicks(body: str, context: str) -> None:
    bad = []
    for onclick_val in _ONCLICK_RE.findall(body):
        if _BROKEN_QUOTED_LITERAL.search(onclick_val):
            bad.append(onclick_val)
    assert not bad, (
        f"Found {len(bad)} broken onclick(s) in {context}: a single-quoted "
        f"JS string literal contains &#39; which the browser will HTML-decode "
        f"to a stray ' and crash JS parsing. Use `{{{{ ... | tojson | "
        f"forceescape }}}}` instead.\nFirst match: {bad[0]!r}"
    )


# ── Asset row fragment endpoint ────────────────────────────────────


@pytest_asyncio.fixture
async def asset_with_apostrophe_and_group(client, app):
    """Seed: a group named ``Mia's group`` + a video asset whose filename
    contains an apostrophe (seeded directly via DB to bypass the upload
    endpoint's filename validation — the test is about HTML rendering,
    not the upload path). Returns the asset id + group id.
    """
    import uuid as _uuid

    from cms.database import get_db
    from cms.models.asset import Asset, AssetType
    from cms.models.device import DeviceGroup
    from cms.models.user import User
    from sqlalchemy import select

    factory = app.dependency_overrides[get_db]
    asset_id = group_id = None
    async for db in factory():
        group = DeviceGroup(name="Mia's group", description="")
        db.add(group)
        await db.flush()
        group_id = str(group.id)

        admin = (
            await db.execute(select(User).where(User.username == "admin"))
        ).scalar_one()

        asset = Asset(
            id=_uuid.uuid4(),
            filename="Mia's clip.mp4",
            original_filename="Mia's clip.mp4",
            display_name="Mia's clip",
            asset_type=AssetType.VIDEO,
            size_bytes=11,
            checksum="0" * 64,
            uploaded_by_user_id=admin.id,
        )
        db.add(asset)
        await db.commit()
        asset_id = str(asset.id)
        break

    return {"asset_id": asset_id, "group_id": group_id}


@pytest.mark.asyncio
class TestAssetRowOnclickEscaping:
    async def test_asset_row_onclicks_are_html_safe(
        self, client, asset_with_apostrophe_and_group
    ):
        asset_id = asset_with_apostrophe_and_group["asset_id"]

        resp = await client.get(f"/api/assets/{asset_id}/row")
        assert resp.status_code == 200, resp.text
        body = resp.text

        # The two onclicks gated on a regular video asset + admin owner +
        # 0 schedules: previewAsset and deleteAsset. Both should now use
        # the safe form.
        assert "previewAsset(" in body
        assert "deleteAsset(" in body
        # Group dropdown for pickAssetGroup renders the seeded group too.
        assert "pickAssetGroup(" in body

        assert_no_broken_onclicks(body, f"/api/assets/{asset_id}/row")

        # Positive: the safe HTML-encoded JSON quote should appear in
        # at least one of the patched onclicks (the filename arg).
        assert "&#34;Mia" in body, (
            "Expected the filename to render with HTML-encoded JSON quotes "
            "after applying `| tojson | forceescape`."
        )


# ── /assets full page ──────────────────────────────────────────────


@pytest_asyncio.fixture
async def group_with_apostrophe(app):
    """Seed a single group named ``Mia's group`` visible to admin."""
    from cms.database import get_db
    from cms.models.device import DeviceGroup

    factory = app.dependency_overrides[get_db]
    group_id = None
    async for db in factory():
        group = DeviceGroup(name="Mia's group", description="")
        db.add(group)
        await db.flush()
        group_id = str(group.id)
        await db.commit()
        break

    return {"group_id": group_id}


@pytest.mark.asyncio
class TestAssetsPageOnclickEscaping:
    async def test_assets_page_group_dropdowns_are_html_safe(
        self, client, group_with_apostrophe
    ):
        resp = await client.get("/assets")
        assert resp.status_code == 200, resp.text
        body = resp.text

        # All three pick*Group handlers should be present on /assets.
        # (They live in the upload-form / webpage-form / stream-form
        # dropdown popups respectively.)
        for handler in ("pickUploadGroup", "pickWebpageGroup", "pickStreamGroup"):
            assert handler + "(" in body, (
                f"{handler} missing from /assets page output -- did the "
                f"upload form structure change?"
            )

        assert_no_broken_onclicks(body, "/assets")


# ── /assets/new/slideshow ──────────────────────────────────────────


@pytest.mark.asyncio
class TestSlideshowBuilderOnclickEscaping:
    async def test_slideshow_builder_group_picker_is_html_safe(
        self, client, group_with_apostrophe
    ):
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text

        assert "pickSsGroup(" in body, (
            "pickSsGroup missing from slideshow builder output -- did the "
            "group picker structure change?"
        )

        assert_no_broken_onclicks(body, "/assets/new/slideshow")


# ── /users full page ───────────────────────────────────────────────


@pytest_asyncio.fixture
async def user_and_role_with_apostrophes(app):
    """Seed an extra user whose email contains an apostrophe and a role
    whose name contains an apostrophe. The default admin already exists
    so the /users page will have at least two user rows.
    """
    from sqlalchemy import select

    from cms.auth import hash_password
    from cms.database import get_db
    from cms.models.user import Role, User

    factory = app.dependency_overrides[get_db]
    user_id = role_id = None
    async for db in factory():
        # Custom role -- name with apostrophe.
        role = Role(name="Mia's role", description="apostrophe role")
        db.add(role)
        await db.flush()
        role_id = str(role.id)

        # User with apostrophe in the email local-part. RFC 5321 allows
        # quoted local-parts that can contain ', so this is valid input.
        user = User(
            username="mia_apos",
            email="mia'apos@example.com",
            display_name="Mia Apostrophe",
            password_hash=hash_password("p"),
            role_id=role.id,
            is_active=True,
            must_change_password=False,
        )
        db.add(user)
        await db.commit()
        user_id = str(user.id)
        break

    return {"user_id": user_id, "role_id": role_id}


@pytest.mark.asyncio
class TestUsersPageOnclickEscaping:
    async def test_users_page_onclicks_are_html_safe(
        self, client, user_and_role_with_apostrophes
    ):
        resp = await client.get("/users")
        assert resp.status_code == 200, resp.text
        body = resp.text

        # All three patched handlers should still render somewhere on
        # the page (admin sees them on the seeded user + role).
        for handler in ("resendInvite", "deleteUser", "deleteRole"):
            assert handler + "(" in body, (
                f"{handler} missing from /users output -- did the kebab "
                f"structure change?"
            )

        assert_no_broken_onclicks(body, "/users")
