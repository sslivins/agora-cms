"""Feature-flag admin API and page.

The evaluation semantics are covered in ``tests/test_feature_flags.py``; this
file covers the surfaces an admin actually touches — the permission boundary,
the listing being registry-driven, and the write path validating ids and
audit-logging.

Every test declares its own registry entry via ``_declare`` rather than relying
on whatever flags happen to be shipped, so these stay stable as real flags come
and go.
"""

import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import hash_password
from cms.models.audit_log import AuditLog
from cms.models.feature_flag import FeatureFlagState
from cms.models.user import Role, User
from cms.permissions import FEATURES_READ, FEATURES_WRITE
from cms.services import feature_flags as ff


FUTURE = date.today() + timedelta(days=30)


@pytest.fixture
def declare():
    """Add flags to the real registry for one test, then remove them."""
    added: list[str] = []

    def _declare(name: str, **kwargs) -> ff.Flag:
        opts = dict(
            description="A test feature",
            owner="platform",
            kind=ff.FlagKind.RELEASE,
            expires=FUTURE,
        )
        opts.update(kwargs)
        flag = ff.Flag(**opts)
        ff.REGISTRY[name] = flag
        added.append(name)
        return flag

    yield _declare
    for name in added:
        ff.REGISTRY.pop(name, None)


async def _role(db: AsyncSession, name: str, permissions: list[str]) -> Role:
    existing = (
        await db.execute(select(Role).where(Role.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        existing.permissions = permissions
        await db.flush()
        return existing
    role = Role(name=name, description=name, permissions=permissions)
    db.add(role)
    await db.flush()
    return role


async def _user(db: AsyncSession, email: str, role: Role) -> User:
    user = User(
        username=email.split("@")[0],
        email=email,
        display_name=email.split("@")[0],
        password_hash=hash_password("password123"),
        role_id=role.id,
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    await db.flush()
    return user


async def _client_for(app, db: AsyncSession, permissions: list[str]) -> AsyncClient:
    """A logged-in client whose role holds exactly ``permissions``."""
    role = await _role(db, "FlagTester", permissions)
    await _user(db, "flagtester@example.com", role)
    await db.commit()
    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    await ac.post(
        "/login",
        data={"username": "flagtester", "password": "password123"},
        follow_redirects=False,
    )
    return ac


# ── Permission boundary ──


@pytest.mark.asyncio
class TestPermissions:
    async def test_listing_requires_features_read(self, app, db_session):
        ac = await _client_for(app, db_session, ["devices:read"])
        resp = await ac.get("/api/features")
        assert resp.status_code == 403
        await ac.aclose()

    async def test_read_permission_cannot_write(self, app, db_session, declare):
        # The split exists so someone can audit what is enabled without being
        # able to change who gets it.
        declare("widget")
        ac = await _client_for(app, db_session, [FEATURES_READ])
        assert (await ac.get("/api/features")).status_code == 200
        resp = await ac.put("/api/features/widget", json={"state": "on"})
        assert resp.status_code == 403
        await ac.aclose()

    async def test_write_permission_can_write(self, app, db_session, declare):
        declare("widget")
        ac = await _client_for(app, db_session, [FEATURES_READ, FEATURES_WRITE])
        resp = await ac.put("/api/features/widget", json={"state": "on"})
        assert resp.status_code == 200
        await ac.aclose()

    async def test_page_requires_features_read(self, app, db_session):
        ac = await _client_for(app, db_session, ["devices:read"])
        resp = await ac.get("/features", follow_redirects=False)
        assert resp.status_code in (302, 303, 403)
        await ac.aclose()

    async def test_page_renders_for_a_reader(self, app, db_session, declare):
        declare("widget")
        ac = await _client_for(app, db_session, [FEATURES_READ])
        resp = await ac.get("/features")
        assert resp.status_code == 200
        assert "Features" in resp.text
        await ac.aclose()

    async def test_nav_tab_follows_the_permission(self, app, db_session, declare):
        declare("widget")
        with_perm = await _client_for(app, db_session, [FEATURES_READ])
        assert 'href="/features"' in (await with_perm.get("/")).text
        await with_perm.aclose()

        # Same user, permission removed: the tab disappears.
        role = (
            await db_session.execute(select(Role).where(Role.name == "FlagTester"))
        ).scalar_one()
        role.permissions = ["devices:read"]
        await db_session.commit()

        without = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        )
        await without.post(
            "/login",
            data={"username": "flagtester", "password": "password123"},
            follow_redirects=False,
        )
        assert 'href="/features"' not in (await without.get("/")).text
        await without.aclose()


# ── Listing ──


@pytest.mark.asyncio
class TestListing:
    async def test_listing_is_registry_driven(self, app, db_session, declare):
        # A newly declared flag must appear with no row and no seeding step.
        declare("widget", description="Shiny new widget", owner="team-a")
        ac = await _client_for(app, db_session, [FEATURES_READ])
        body = (await ac.get("/api/features")).json()
        entry = next(f for f in body["flags"] if f["name"] == "widget")
        assert entry["description"] == "Shiny new widget"
        assert entry["owner"] == "team-a"
        assert entry["state"] == "off"
        assert entry["is_default"] is True
        await ac.aclose()

    async def test_listing_includes_catalogs_for_the_picker(
        self, app, db_session, declare
    ):
        # Targeting is stored by id but must never be administered by id.
        declare("widget")
        ac = await _client_for(app, db_session, [FEATURES_READ])
        body = (await ac.get("/api/features")).json()
        assert any(u["username"] == "flagtester" for u in body["users"])
        assert any(r["name"] == "FlagTester" for r in body["roles"])
        await ac.aclose()

    async def test_listing_surfaces_an_overdue_release_toggle(
        self, app, db_session, declare
    ):
        declare("stale", expires=date.today() - timedelta(days=1))
        ac = await _client_for(app, db_session, [FEATURES_READ])
        body = (await ac.get("/api/features")).json()
        entry = next(f for f in body["flags"] if f["name"] == "stale")
        assert entry["is_overdue"] is True
        await ac.aclose()

    async def test_listing_reports_the_admin_preview_opt_in(
        self, app, db_session, declare
    ):
        # Surfaced so an admin isn't left guessing why they can still see a
        # feature that is targeted away from them.
        declare("widget", include_admins=True)
        declare("gadget", include_admins=False)
        ac = await _client_for(app, db_session, [FEATURES_READ])
        flags = {f["name"]: f for f in (await ac.get("/api/features")).json()["flags"]}
        assert flags["widget"]["include_admins"] is True
        assert flags["gadget"]["include_admins"] is False
        await ac.aclose()


# ── Writes ──


@pytest.mark.asyncio
class TestWrites:
    async def test_setting_targeted_stores_users_and_roles(
        self, app, db_session, declare
    ):
        declare("widget")
        ac = await _client_for(app, db_session, [FEATURES_READ, FEATURES_WRITE])
        target_role = await _role(db_session, "Pilots", [])
        target_user = await _user(db_session, "pilot@example.com", target_role)
        await db_session.commit()

        resp = await ac.put(
            "/api/features/widget",
            json={
                "state": "targeted",
                "user_ids": [str(target_user.id)],
                "role_ids": [str(target_role.id)],
            },
        )
        assert resp.status_code == 200
        entry = next(f for f in resp.json()["flags"] if f["name"] == "widget")
        assert entry["state"] == "targeted"
        assert entry["user_ids"] == [str(target_user.id)]
        assert entry["role_ids"] == [str(target_role.id)]
        assert entry["is_default"] is False
        await ac.aclose()

    async def test_response_reflects_stored_state_not_the_request(
        self, app, db_session, declare
    ):
        # The service clears targeting when leaving "targeted"; the response
        # must show that so the page can't display a stale allowlist.
        declare("widget")
        ac = await _client_for(app, db_session, [FEATURES_READ, FEATURES_WRITE])
        target_role = await _role(db_session, "Pilots", [])
        await db_session.commit()

        await ac.put(
            "/api/features/widget",
            json={"state": "targeted", "role_ids": [str(target_role.id)]},
        )
        resp = await ac.put(
            "/api/features/widget",
            json={"state": "off", "role_ids": [str(target_role.id)]},
        )
        entry = next(f for f in resp.json()["flags"] if f["name"] == "widget")
        assert entry["state"] == "off"
        assert entry["role_ids"] == []
        await ac.aclose()

    async def test_unknown_user_id_is_rejected_whole(self, app, db_session, declare):
        # Silently dropping an id looks to the admin like the grant worked,
        # and the person it was meant for never gets the feature.
        declare("widget")
        ac = await _client_for(app, db_session, [FEATURES_READ, FEATURES_WRITE])
        ghost = str(uuid.uuid4())
        resp = await ac.put(
            "/api/features/widget",
            json={"state": "targeted", "user_ids": [ghost]},
        )
        assert resp.status_code == 400
        assert ghost in resp.json()["detail"]["unknown_user_ids"]
        assert await db_session.get(FeatureFlagState, "widget") is None
        await ac.aclose()

    async def test_unknown_role_id_is_rejected(self, app, db_session, declare):
        declare("widget")
        ac = await _client_for(app, db_session, [FEATURES_READ, FEATURES_WRITE])
        resp = await ac.put(
            "/api/features/widget",
            json={"state": "targeted", "role_ids": [str(uuid.uuid4())]},
        )
        assert resp.status_code == 400
        await ac.aclose()

    async def test_unknown_flag_is_404(self, app, db_session, declare):
        ac = await _client_for(app, db_session, [FEATURES_READ, FEATURES_WRITE])
        resp = await ac.put("/api/features/ghost", json={"state": "on"})
        assert resp.status_code == 404
        await ac.aclose()

    async def test_unknown_state_is_400(self, app, db_session, declare):
        declare("widget")
        ac = await _client_for(app, db_session, [FEATURES_READ, FEATURES_WRITE])
        resp = await ac.put("/api/features/widget", json={"state": "maybe"})
        assert resp.status_code == 400
        assert "off" in resp.json()["detail"]["allowed"]
        await ac.aclose()

    async def test_change_is_audit_logged(self, app, db_session, declare):
        # Flag flips belong in the CMS audit log alongside every other
        # administrative change, not in a separate system.
        declare("widget")
        ac = await _client_for(app, db_session, [FEATURES_READ, FEATURES_WRITE])
        await ac.put("/api/features/widget", json={"state": "on"})

        entry = (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "features.state.update")
            )
        ).scalars().first()
        assert entry is not None
        assert entry.resource_id == "widget"
        assert entry.details["state"] == "on"
        await ac.aclose()
