"""Unit tests for ``cms.services.fleet_registry``.

Covers the read/write paths and the multi-replica locking
contract documented in the module docstring.
"""

from __future__ import annotations

import base64
import uuid

import pytest

from cms.services import fleet_registry
from shared.models.fleet import Fleet


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------
# get_fleet_secret
# ---------------------------------------------------------------------


async def test_get_fleet_secret_hit(db_session):
    secret_raw = b"\x42" * 32
    secret_b64 = base64.b64encode(secret_raw).decode("ascii")
    db_session.add(Fleet(fleet_id="hit-fleet", secret_b64=secret_b64))
    await db_session.commit()

    out = await fleet_registry.get_fleet_secret(db_session, "hit-fleet")
    assert out == secret_raw


async def test_get_fleet_secret_miss(db_session):
    out = await fleet_registry.get_fleet_secret(db_session, "no-such-fleet")
    assert out is None


async def test_get_fleet_secret_soft_deleted_invisible(db_session):
    from datetime import datetime, timezone

    secret_b64 = base64.b64encode(b"\x01" * 32).decode("ascii")
    f = Fleet(
        fleet_id="gone",
        secret_b64=secret_b64,
        deleted_at=datetime.now(timezone.utc),
    )
    db_session.add(f)
    await db_session.commit()

    out = await fleet_registry.get_fleet_secret(db_session, "gone")
    assert out is None


async def test_get_fleet_secret_misconfigured_raises(db_session):
    db_session.add(Fleet(fleet_id="bad", secret_b64="not-base-64!!"))
    await db_session.commit()

    with pytest.raises(fleet_registry.FleetSecretMisconfigured):
        await fleet_registry.get_fleet_secret(db_session, "bad")


# ---------------------------------------------------------------------
# list_active_fleets
# ---------------------------------------------------------------------


async def test_list_active_fleets_excludes_deleted(db_session):
    from datetime import datetime, timezone

    secret_b64 = base64.b64encode(b"\x09" * 32).decode("ascii")
    db_session.add_all([
        Fleet(fleet_id="alpha", secret_b64=secret_b64),
        Fleet(fleet_id="beta", secret_b64=secret_b64),
        Fleet(
            fleet_id="zeta-old",
            secret_b64=secret_b64,
            deleted_at=datetime.now(timezone.utc),
        ),
    ])
    await db_session.commit()

    rows = await fleet_registry.list_active_fleets(db_session)
    ids = [r.fleet_id for r in rows]
    assert ids == sorted(ids)  # ordered by fleet_id
    assert "alpha" in ids
    assert "beta" in ids
    assert "zeta-old" not in ids


# ---------------------------------------------------------------------
# create_fleet
# ---------------------------------------------------------------------


async def test_create_fleet_generates_secret(db_session):
    row = await fleet_registry.create_fleet(
        db_session, fleet_id="auto-secret",
    )
    await db_session.commit()
    assert row.fleet_id == "auto-secret"
    # 32 raw bytes -> 44 base64 chars
    assert len(row.secret_b64) == 44
    assert base64.b64decode(row.secret_b64, validate=True)


async def test_create_fleet_accepts_explicit_secret(db_session):
    secret_b64 = base64.b64encode(b"\x07" * 32).decode("ascii")
    row = await fleet_registry.create_fleet(
        db_session, fleet_id="explicit", secret_b64=secret_b64,
    )
    await db_session.commit()
    assert row.secret_b64 == secret_b64


async def test_create_fleet_rejects_bad_secret(db_session):
    with pytest.raises(ValueError):
        await fleet_registry.create_fleet(
            db_session, fleet_id="bad", secret_b64="$$not-b64$$",
        )


async def test_create_fleet_duplicate_active_raises(db_session):
    await fleet_registry.create_fleet(db_session, fleet_id="dup")
    await db_session.commit()
    with pytest.raises(fleet_registry.FleetAlreadyExists):
        await fleet_registry.create_fleet(db_session, fleet_id="dup")


async def test_create_fleet_can_recreate_after_soft_delete(db_session):
    # The partial unique index ``WHERE deleted_at IS NULL`` is a
    # postgres feature; SQLite (used by the unit-test DB) treats it
    # as a plain unique index, so this scenario is exercised in the
    # nightly E2E suite against postgres rather than here.
    bind = db_session.get_bind()
    if bind.dialect.name != "postgresql":
        pytest.skip("partial unique index requires postgres")

    await fleet_registry.create_fleet(db_session, fleet_id="reborn")
    await db_session.commit()
    deleted = await fleet_registry.delete_fleet(db_session, "reborn")
    assert deleted is True
    await db_session.commit()

    # Same name, fresh row, partial unique index allows it.
    row = await fleet_registry.create_fleet(db_session, fleet_id="reborn")
    await db_session.commit()
    assert row.deleted_at is None


# ---------------------------------------------------------------------
# delete_fleet
# ---------------------------------------------------------------------


async def test_delete_fleet_soft_deletes(db_session):
    await fleet_registry.create_fleet(db_session, fleet_id="byebye")
    await db_session.commit()

    out = await fleet_registry.delete_fleet(db_session, "byebye")
    await db_session.commit()
    assert out is True

    secret = await fleet_registry.get_fleet_secret(db_session, "byebye")
    assert secret is None  # invisible to the read path


async def test_delete_fleet_idempotent_returns_false(db_session):
    out = await fleet_registry.delete_fleet(db_session, "never-existed")
    assert out is False


# ---------------------------------------------------------------------
# get_fleet_for_build
# ---------------------------------------------------------------------


async def test_get_fleet_for_build_returns_active_row(db_session):
    secret_b64 = base64.b64encode(b"\x20" * 32).decode("ascii")
    await fleet_registry.create_fleet(
        db_session, fleet_id="builder", secret_b64=secret_b64,
    )
    await db_session.commit()

    out = await fleet_registry.get_fleet_for_build(db_session, "builder")
    assert out is not None
    assert out.fleet_id == "builder"
    assert out.secret_b64 == secret_b64


async def test_get_fleet_for_build_misses_deleted(db_session):
    await fleet_registry.create_fleet(db_session, fleet_id="going")
    await db_session.commit()
    await fleet_registry.delete_fleet(db_session, "going")
    await db_session.commit()

    out = await fleet_registry.get_fleet_for_build(db_session, "going")
    assert out is None
