"""Migration policy enforcement tests (#298).

Project policy: Alembic ``downgrade()`` functions are intentionally
**unsupported**.  Rollbacks are forbidden — if you need to revert a
schema change, write a new forward migration that undoes it.

This is enforced at runtime by every ``downgrade()`` raising
``NotImplementedError``.  These tests make that a hard-coded
contract so someone can't silently add a half-baked downgrade that
gets relied on in an incident.

Rationale for tightening this into CI:
- The alternative is "catch a broken downgrade during a production
  rollback", which is the worst possible time.
- A real, tested, production-safe downgrade is a LOT of work
  (foreign-key teardown order, back-fill on re-upgrade, etc.).  The
  project has chosen to pay the cost on forward migrations only.
- If the policy is ever revisited, revisit these tests too.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MIGRATIONS_DIR = Path(__file__).parent.parent / "alembic" / "versions"


def _migration_files() -> list[Path]:
    return sorted(p for p in MIGRATIONS_DIR.glob("*.py") if not p.name.startswith("_"))


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migrations_dir_exists():
    assert MIGRATIONS_DIR.exists(), f"alembic versions dir missing: {MIGRATIONS_DIR}"


def test_at_least_one_migration_exists():
    assert _migration_files(), "no migrations found in alembic/versions"


@pytest.mark.parametrize("mig_path", _migration_files(), ids=lambda p: p.name)
def test_migration_defines_upgrade_and_downgrade(mig_path: Path):
    mod = _load(mig_path)
    assert callable(getattr(mod, "upgrade", None)), f"{mig_path.name}: missing upgrade()"
    assert callable(getattr(mod, "downgrade", None)), f"{mig_path.name}: missing downgrade()"


@pytest.mark.parametrize("mig_path", _migration_files(), ids=lambda p: p.name)
def test_downgrade_raises_not_implemented(mig_path: Path):
    """Project policy: every downgrade() must raise NotImplementedError.

    Prevents accidental half-implemented rollback paths from sneaking in.
    If this test fails, you've probably tried to implement a real
    downgrade — talk to the maintainer before removing this guard.
    """
    mod = _load(mig_path)
    with pytest.raises(NotImplementedError):
        mod.downgrade()


@pytest.mark.parametrize("mig_path", _migration_files(), ids=lambda p: p.name)
def test_migration_has_revision_id(mig_path: Path):
    mod = _load(mig_path)
    assert getattr(mod, "revision", None), f"{mig_path.name}: missing revision ID"
    assert hasattr(mod, "down_revision"), f"{mig_path.name}: missing down_revision attribute"


def test_revision_ids_unique():
    """Two migrations can't share a revision ID — alembic picks one arbitrarily."""
    ids: dict[str, str] = {}
    for p in _migration_files():
        mod = _load(p)
        rev = mod.revision
        if rev in ids:
            pytest.fail(f"duplicate revision id {rev!r} in {p.name} and {ids[rev]}")
        ids[rev] = p.name


def test_revision_chain_is_linear():
    """down_revision chain forms a single line from None → head (no forks/merges)."""
    mods = [_load(p) for p in _migration_files()]
    by_rev = {m.revision: m for m in mods}

    # Exactly one root (down_revision is None).
    roots = [m.revision for m in mods if m.down_revision is None]
    assert len(roots) == 1, f"expected exactly 1 root migration, got {roots}"

    # Every non-root down_revision must reference a known revision.
    for m in mods:
        if m.down_revision is None:
            continue
        # down_revision can be a string or a tuple (merge points).
        # We forbid merges: should be a single string.
        assert isinstance(m.down_revision, str), (
            f"{m.revision}: down_revision is a tuple ({m.down_revision!r}) — "
            "merge migrations are not allowed"
        )
        assert m.down_revision in by_rev, (
            f"{m.revision}: references unknown down_revision {m.down_revision!r}"
        )

    # No two migrations share a down_revision (that's a fork).
    parents: dict[str, str] = {}
    for m in mods:
        if m.down_revision is None:
            continue
        if m.down_revision in parents:
            pytest.fail(
                f"fork detected: {m.revision} and {parents[m.down_revision]} "
                f"both have down_revision={m.down_revision!r}"
            )
        parents[m.down_revision] = m.revision
