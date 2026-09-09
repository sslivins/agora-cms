"""Feature-flag registry and evaluation.

These cover the semantics the design signed off on — three states rather than a
boolean, off meaning off even for admins unless a flag opts in, targeting by
user *and* role, an absent row falling back to the declared default, and
per-request memoisation — because each of those is a decision that a later
refactor could quietly undo.

The tests inject their own registry rather than mutating the module-level one,
so they neither depend on nor disturb whichever flags happen to be declared.
"""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import hash_password
from cms.models.feature_flag import FeatureFlagState
from cms.models.user import Role, User
from cms.permissions import SETTINGS_WRITE
from cms.services import feature_flags as ff


FUTURE = date.today() + timedelta(days=30)


# ── Helpers ──


async def _role(db: AsyncSession, name: str, permissions: list[str]) -> Role:
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


def _registry(**overrides) -> dict[str, ff.Flag]:
    base = dict(
        description="A test feature",
        owner="platform",
        kind=ff.FlagKind.RELEASE,
        expires=FUTURE,
    )
    base.update(overrides)
    return {"widget": ff.Flag(**base)}


class _FakeRequest:
    """Stand-in for a Starlette request: all `enabled()` touches is `.state`."""

    class _State:
        pass

    def __init__(self):
        self.state = self._State()


# ── Declaration ──


class TestFlagDeclaration:
    def test_release_flag_requires_an_expiry(self):
        # Without this, a temporary rollout toggle becomes permanent by
        # accident and nothing ever prompts its removal.
        with pytest.raises(ValueError, match="expiry"):
            ff.Flag(description="d", owner="o", kind=ff.FlagKind.RELEASE)

    def test_permanent_flag_needs_no_expiry(self):
        flag = ff.Flag(description="d", owner="o", kind=ff.FlagKind.PERMANENT)
        assert flag.expires is None

    def test_default_is_off_and_admins_are_not_included(self):
        flag = ff.Flag(description="d", owner="o", kind=ff.FlagKind.PERMANENT)
        assert flag.default is ff.FlagState.OFF
        assert flag.include_admins is False

    def test_registry_release_flags_are_not_overdue(self):
        # Guards the real registry, not a fixture: a shipped release toggle
        # that is past its expiry is owed a cleanup.
        overdue = [
            name
            for name, flag in ff.REGISTRY.items()
            if flag.kind is ff.FlagKind.RELEASE
            and flag.expires is not None
            and flag.expires < date.today()
        ]
        assert overdue == [], f"release flags past their expiry: {overdue}"


# ── Evaluation ──


@pytest.mark.asyncio
class TestEvaluation:
    async def test_absent_row_uses_the_declared_default(self, db_session):
        reg = _registry(default=ff.FlagState.ON)
        assert await ff.enabled(db_session, "widget", None, registry=reg) is True

        reg = _registry(default=ff.FlagState.OFF)
        assert await ff.enabled(db_session, "widget", None, registry=reg) is False

    async def test_unknown_flag_is_off_rather_than_raising(self, db_session, caplog):
        # A live page must not 500 because a registry entry was removed while a
        # call site still references it.
        assert await ff.enabled(db_session, "no-such-flag", None, registry={}) is False
        assert "no-such-flag" in caplog.text

    async def test_on_reaches_an_anonymous_caller(self, db_session):
        reg = _registry()
        await ff.set_state(db_session, "widget", state=ff.FlagState.ON, registry=reg)
        assert await ff.enabled(db_session, "widget", None, registry=reg) is True

    async def test_off_beats_targeting(self, db_session):
        reg = _registry()
        role = await _role(db_session, "Pilot", [])
        user = await _user(db_session, "pilot@example.com", role)
        await ff.set_state(
            db_session, "widget", state=ff.FlagState.TARGETED,
            user_ids=[user.id], registry=reg,
        )
        await ff.set_state(db_session, "widget", state=ff.FlagState.OFF, registry=reg)
        assert await ff.enabled(db_session, "widget", user, registry=reg) is False

    async def test_targeted_user_is_in_and_others_are_out(self, db_session):
        reg = _registry()
        role = await _role(db_session, "Pilot", [])
        alice = await _user(db_session, "alice@example.com", role)
        bob = await _user(db_session, "bob@example.com", role)
        await ff.set_state(
            db_session, "widget", state=ff.FlagState.TARGETED,
            user_ids=[alice.id], registry=reg,
        )
        assert await ff.enabled(db_session, "widget", alice, registry=reg) is True
        assert await ff.enabled(db_session, "widget", bob, registry=reg) is False

    async def test_targeted_role_grants_every_member(self, db_session):
        reg = _registry()
        pilots = await _role(db_session, "Pilot", [])
        others = await _role(db_session, "Other", [])
        member = await _user(db_session, "member@example.com", pilots)
        outsider = await _user(db_session, "outsider@example.com", others)
        await ff.set_state(
            db_session, "widget", state=ff.FlagState.TARGETED,
            role_ids=[pilots.id], registry=reg,
        )
        assert await ff.enabled(db_session, "widget", member, registry=reg) is True
        assert await ff.enabled(db_session, "widget", outsider, registry=reg) is False

    async def test_targeted_excludes_anonymous(self, db_session):
        reg = _registry()
        await ff.set_state(
            db_session, "widget", state=ff.FlagState.TARGETED, registry=reg
        )
        assert await ff.enabled(db_session, "widget", None, registry=reg) is False

    async def test_admins_are_not_included_by_default(self, db_session):
        # The headline semantic: off means off, and targeted means targeted,
        # even for someone holding settings:write.
        reg = _registry()
        admin_role = await _role(db_session, "Admin", [SETTINGS_WRITE])
        admin = await _user(db_session, "admin@example.com", admin_role)
        await ff.set_state(
            db_session, "widget", state=ff.FlagState.TARGETED, registry=reg
        )
        assert await ff.enabled(db_session, "widget", admin, registry=reg) is False

    async def test_include_admins_opts_a_flag_into_the_hatch(self, db_session):
        reg = _registry(include_admins=True)
        admin_role = await _role(db_session, "Admin", [SETTINGS_WRITE])
        plain_role = await _role(db_session, "Viewer", ["assets:read"])
        admin = await _user(db_session, "admin@example.com", admin_role)
        viewer = await _user(db_session, "viewer@example.com", plain_role)
        await ff.set_state(
            db_session, "widget", state=ff.FlagState.TARGETED, registry=reg
        )
        assert await ff.enabled(db_session, "widget", admin, registry=reg) is True
        assert await ff.enabled(db_session, "widget", viewer, registry=reg) is False

    async def test_include_admins_does_not_override_off(self, db_session):
        reg = _registry(include_admins=True)
        admin_role = await _role(db_session, "Admin", [SETTINGS_WRITE])
        admin = await _user(db_session, "admin@example.com", admin_role)
        await ff.set_state(db_session, "widget", state=ff.FlagState.OFF, registry=reg)
        assert await ff.enabled(db_session, "widget", admin, registry=reg) is False

    async def test_admin_hatch_works_when_role_is_not_eager_loaded(self, db_session):
        # Call sites differ in whether they loaded User.role; touching an
        # unloaded relationship on an async session raises MissingGreenlet, so
        # the service must fetch it rather than assume.
        reg = _registry(include_admins=True)
        admin_role = await _role(db_session, "Admin", [SETTINGS_WRITE])
        await _user(db_session, "admin@example.com", admin_role)
        await ff.set_state(
            db_session, "widget", state=ff.FlagState.TARGETED, registry=reg
        )
        await db_session.commit()
        # expunge rather than expire: the re-selected User must be a genuinely
        # fresh instance with `role` unloaded, which is the case under test.
        db_session.expunge_all()

        fresh = (
            await db_session.execute(
                select(User).where(User.email == "admin@example.com")
            )
        ).scalar_one()
        assert await ff.enabled(db_session, "widget", fresh, registry=reg) is True

    async def test_unrecognised_stored_state_falls_back_to_the_default(
        self, db_session, caplog
    ):
        reg = _registry(default=ff.FlagState.ON)
        db_session.add(
            FeatureFlagState(
                name="widget", state="bogus", user_ids=[], role_ids=[],
                updated_at=datetime.now(timezone.utc),
            )
        )
        await db_session.flush()
        assert await ff.enabled(db_session, "widget", None, registry=reg) is True
        assert "bogus" in caplog.text


# ── Caching ──


@pytest.mark.asyncio
class TestPerRequestMemoisation:
    async def test_repeat_checks_hit_the_cache(self, db_session):
        reg = _registry(default=ff.FlagState.ON)
        request = _FakeRequest()
        assert await ff.enabled(
            db_session, "widget", None, request=request, registry=reg
        ) is True

        # Flip the stored state behind the cache's back; the same request must
        # keep its answer, which is what makes the memoisation observable.
        await ff.set_state(db_session, "widget", state=ff.FlagState.OFF, registry=reg)
        assert await ff.enabled(
            db_session, "widget", None, request=request, registry=reg
        ) is True

        # A different request sees the new value: the cache is per-request, so
        # there is nothing to invalidate across replicas.
        assert await ff.enabled(
            db_session, "widget", None, request=_FakeRequest(), registry=reg
        ) is False

    async def test_cache_is_keyed_by_user(self, db_session):
        reg = _registry()
        role = await _role(db_session, "Pilot", [])
        alice = await _user(db_session, "alice@example.com", role)
        bob = await _user(db_session, "bob@example.com", role)
        await ff.set_state(
            db_session, "widget", state=ff.FlagState.TARGETED,
            user_ids=[alice.id], registry=reg,
        )
        request = _FakeRequest()
        assert await ff.enabled(
            db_session, "widget", alice, request=request, registry=reg
        ) is True
        assert await ff.enabled(
            db_session, "widget", bob, request=request, registry=reg
        ) is False


# ── Administration ──


@pytest.mark.asyncio
class TestAdministration:
    async def test_set_state_records_who_changed_it(self, db_session):
        reg = _registry()
        role = await _role(db_session, "Admin", [SETTINGS_WRITE])
        actor = await _user(db_session, "admin@example.com", role)
        view = await ff.set_state(
            db_session, "widget", state=ff.FlagState.ON, actor=actor, registry=reg
        )
        assert view.state is ff.FlagState.ON
        assert view.updated_by_id == actor.id
        assert view.is_default is False

    async def test_switching_away_from_targeted_clears_targeting(self, db_session):
        # Otherwise the UI shows a stale allowlist attached to a flag that is
        # off for everyone, which reads as "these people still have it".
        reg = _registry()
        role = await _role(db_session, "Pilot", [])
        user = await _user(db_session, "pilot@example.com", role)
        await ff.set_state(
            db_session, "widget", state=ff.FlagState.TARGETED,
            user_ids=[user.id], role_ids=[role.id], registry=reg,
        )
        view = await ff.set_state(
            db_session, "widget", state=ff.FlagState.OFF, registry=reg
        )
        assert view.user_ids == []
        assert view.role_ids == []

    async def test_set_state_rejects_an_undeclared_flag(self, db_session):
        with pytest.raises(KeyError):
            await ff.set_state(
                db_session, "ghost", state=ff.FlagState.ON, registry={}
            )
        # And nothing was written.
        assert await db_session.get(FeatureFlagState, "ghost") is None

    async def test_set_state_is_idempotent_on_the_same_row(self, db_session):
        reg = _registry()
        await ff.set_state(db_session, "widget", state=ff.FlagState.ON, registry=reg)
        await ff.set_state(db_session, "widget", state=ff.FlagState.OFF, registry=reg)
        rows = (await db_session.execute(select(FeatureFlagState))).scalars().all()
        assert len(rows) == 1
        assert rows[0].state == "off"

    async def test_get_all_is_driven_by_the_registry_not_the_table(self, db_session):
        # A newly declared flag must appear immediately, with no seeding step.
        reg = _registry()
        reg["gadget"] = ff.Flag(
            description="Another", owner="platform", kind=ff.FlagKind.PERMANENT,
            default=ff.FlagState.ON,
        )
        views = await ff.get_all(db_session, registry=reg)
        assert [v.name for v in views] == ["gadget", "widget"]
        assert all(v.is_default for v in views)
        assert {v.name: v.state for v in views} == {
            "gadget": ff.FlagState.ON,
            "widget": ff.FlagState.OFF,
        }

    async def test_get_returns_none_for_an_undeclared_flag(self, db_session):
        assert await ff.get(db_session, "ghost", registry={}) is None

    async def test_view_reports_an_overdue_release_toggle(self):
        stale = ff.FlagView(
            name="widget", description="d", owner="o", kind=ff.FlagKind.RELEASE,
            state=ff.FlagState.ON, expires=date.today() - timedelta(days=1),
        )
        assert stale.is_overdue is True

        permanent = ff.FlagView(
            name="switch", description="d", owner="o", kind=ff.FlagKind.PERMANENT,
            state=ff.FlagState.ON, expires=None,
        )
        assert permanent.is_overdue is False


# ── Storage ──


@pytest.mark.asyncio
class TestStorage:
    async def test_deleting_the_actor_keeps_the_flag(self, db_session):
        # SET NULL, not CASCADE: removing a departing admin must not silently
        # revert every feature they turned on.
        reg = _registry()
        role = await _role(db_session, "Admin", [SETTINGS_WRITE])
        actor = await _user(db_session, "admin@example.com", role)
        await ff.set_state(
            db_session, "widget", state=ff.FlagState.ON, actor=actor, registry=reg
        )
        await db_session.commit()

        await db_session.delete(actor)
        await db_session.commit()
        db_session.expunge_all()

        row = await db_session.get(FeatureFlagState, "widget")
        assert row is not None
        assert row.state == "on"
        assert row.updated_by_id is None

    async def test_targeting_round_trips_as_uuid_strings(self, db_session):
        reg = _registry()
        role = await _role(db_session, "Pilot", [])
        user = await _user(db_session, "pilot@example.com", role)
        await ff.set_state(
            db_session, "widget", state=ff.FlagState.TARGETED,
            user_ids=[user.id], role_ids=[role.id], registry=reg,
        )
        await db_session.commit()
        db_session.expunge_all()

        row = await db_session.get(FeatureFlagState, "widget")
        assert row.user_ids == [str(user.id)]
        assert row.role_ids == [str(role.id)]
        # Accepts already-stringified ids too, since the API layer will pass
        # whatever the JSON body produced.
        assert isinstance(uuid.UUID(row.user_ids[0]), uuid.UUID)
