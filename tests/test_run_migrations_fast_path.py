"""Unit tests for ``cms.database.run_migrations`` branch selection.

These are DB-independent: the application engine, the SQLAlchemy
inspector, alembic's ``ScriptDirectory``, and alembic's
``command.upgrade`` / ``command.stamp`` are all faked/spied so we can
assert exactly which path ``run_migrations`` takes for each DB state.

The behaviour under test is the "already at head" fast path added to
avoid spinning up alembic's own (second) async engine on every boot —
that second engine has been observed to hang on freshly-scheduled
Azure Container Apps replicas, wedging revision activation.
"""

from __future__ import annotations

import pytest

import cms.database as dbmod


# ── Fakes ──────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSyncConn:
    """Stand-in for the sync connection handed to ``run_sync`` callbacks."""

    def __init__(self, versions):
        self._versions = versions

    def exec_driver_sql(self, _sql):
        return _FakeResult([(v,) for v in self._versions])


class _FakeInspector:
    def __init__(self, tables):
        self._tables = tables

    def has_table(self, name):
        return name in self._tables


class _FakeAsyncConn:
    def __init__(self, sync_conn):
        self._sync = sync_conn

    async def run_sync(self, fn, *args):
        return fn(self._sync, *args)


class _FakeBegin:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_a):
        return False


class _FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    def begin(self):
        return _FakeBegin(self._conn)

    def connect(self):
        return _FakeBegin(self._conn)


class _FakeScriptDirectory:
    _heads: list[str] = []

    @classmethod
    def from_config(cls, _cfg):
        inst = cls()
        return inst

    def get_heads(self):
        return list(type(self)._heads)


# ── Harness ────────────────────────────────────────────────────────────


def _install(monkeypatch, *, tables, versions, heads):
    """Wire up the fakes and return a dict recording alembic command calls."""
    calls: dict = {"upgrade": 0, "stamp": 0, "injected": []}

    sync_conn = _FakeSyncConn(versions)
    engine = _FakeEngine(_FakeAsyncConn(sync_conn))
    calls["sync_conn"] = sync_conn
    monkeypatch.setattr(dbmod._shared_db, "_engine", engine)
    monkeypatch.setattr(dbmod, "sa_inspect", lambda _c: _FakeInspector(tables))

    _FakeScriptDirectory._heads = list(heads)
    import alembic.script

    monkeypatch.setattr(alembic.script, "ScriptDirectory", _FakeScriptDirectory)

    import alembic.command

    def _fake_upgrade(cfg, _rev):
        calls["upgrade"] += 1
        calls["injected"].append(cfg.attributes.get("connection"))

    def _fake_stamp(cfg, _rev):
        calls["stamp"] += 1
        calls["injected"].append(cfg.attributes.get("connection"))

    monkeypatch.setattr(alembic.command, "upgrade", _fake_upgrade)
    monkeypatch.setattr(alembic.command, "stamp", _fake_stamp)
    return calls


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_upgrade_when_already_at_head(monkeypatch):
    calls = _install(
        monkeypatch,
        tables={"alembic_version", "assets"},
        versions=["0040_head"],
        heads=["0040_head"],
    )
    await dbmod.run_migrations()
    assert calls["upgrade"] == 0
    assert calls["stamp"] == 0


@pytest.mark.asyncio
async def test_runs_upgrade_when_behind_head(monkeypatch):
    calls = _install(
        monkeypatch,
        tables={"alembic_version", "assets"},
        versions=["0039_old"],
        heads=["0040_head"],
    )
    await dbmod.run_migrations()
    assert calls["upgrade"] == 1
    assert calls["stamp"] == 0
    # The app's primary connection is injected so alembic does NOT build a
    # second engine (the historical ACA activation hang).
    assert calls["injected"] == [calls["sync_conn"]]


@pytest.mark.asyncio
async def test_multi_head_order_independent(monkeypatch):
    # Two heads recorded in a different order than the filesystem reports;
    # set comparison must treat them as equal and skip.
    calls = _install(
        monkeypatch,
        tables={"alembic_version"},
        versions=["b_head", "a_head"],
        heads=["a_head", "b_head"],
    )
    await dbmod.run_migrations()
    assert calls["upgrade"] == 0
    assert calls["stamp"] == 0


@pytest.mark.asyncio
async def test_partial_multi_head_runs_upgrade(monkeypatch):
    # DB at one of two heads -> not fully merged -> must upgrade.
    calls = _install(
        monkeypatch,
        tables={"alembic_version"},
        versions=["a_head"],
        heads=["a_head", "b_head"],
    )
    await dbmod.run_migrations()
    assert calls["upgrade"] == 1


@pytest.mark.asyncio
async def test_legacy_schema_is_stamped(monkeypatch):
    # No alembic_version but a legacy assets table -> stamp, never upgrade.
    calls = _install(
        monkeypatch,
        tables={"assets"},
        versions=[],
        heads=["0040_head"],
    )
    await dbmod.run_migrations()
    assert calls["stamp"] == 1
    assert calls["upgrade"] == 0
    assert calls["injected"] == [calls["sync_conn"]]


@pytest.mark.asyncio
async def test_fresh_db_runs_upgrade(monkeypatch):
    # Neither marker present -> fresh DB -> upgrade builds everything.
    calls = _install(
        monkeypatch,
        tables=set(),
        versions=[],
        heads=["0040_head"],
    )
    await dbmod.run_migrations()
    assert calls["upgrade"] == 1
    assert calls["stamp"] == 0


@pytest.mark.asyncio
async def test_empty_version_table_runs_upgrade(monkeypatch):
    # alembic_version exists but is empty -> don't skip; let alembic decide.
    calls = _install(
        monkeypatch,
        tables={"alembic_version"},
        versions=[],
        heads=["0040_head"],
    )
    await dbmod.run_migrations()
    assert calls["upgrade"] == 1
