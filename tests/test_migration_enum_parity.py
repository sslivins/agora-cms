"""Regression fence: Postgres enum types must cover every Python enum member.

SQLAlchemy persists enum *names* (uppercase), not ``.value`` strings, so every
member added to a Python enum backed by a native Postgres enum needs a
corresponding ``ALTER TYPE ... ADD VALUE '<NAME>'`` in a migration.

This class of bug has shipped three times:

* 0037 added ``assettype`` value ``'composed'`` (lowercase) -> every insert
  failed until 0038 added ``'COMPOSED'``.
* 0063 added ``AssetType.VOICE_ANNOUNCEMENT`` correctly but forgot the
  ``jobtype`` enum entirely for ``JobType.VOICE_SYNTHESIS``, so every
  voice-announcement create failed at ``enqueue_job`` with
  ``invalid input value for enum jobtype: "VOICE_SYNTHESIS"`` -> fixed in 0064.

The unit-test suite runs on SQLite, where these enums are CHECK constraints
generated from the Python enum at fixture time, so it can never catch the gap.
This test instead statically parses the migration scripts, making it
DB-agnostic and effective in ordinary CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from shared.models.asset import AssetType
from shared.models.job import JobStatus, JobType


_VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _migration_sources() -> list[str]:
    files = sorted(_VERSIONS_DIR.glob("*.py"))
    assert files, f"no migration scripts found under {_VERSIONS_DIR}"
    return [f.read_text(encoding="utf-8") for f in files]


def _values_defined_for(enum_name: str) -> set[str]:
    """Collect every value a migration declares for a native Postgres enum."""
    found: set[str] = set()
    sources = _migration_sources()

    # Creation form: sa.Enum('A', 'B', name='jobtype')
    create_re = re.compile(
        r"sa\.Enum\(\s*(?P<values>(?:\s*['\"][^'\"]+['\"]\s*,\s*)*)"
        rf"name\s*=\s*['\"]{re.escape(enum_name)}['\"]",
        re.MULTILINE,
    )
    # Extension form: ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'C'
    alter_re = re.compile(
        rf"ALTER\s+TYPE\s+{re.escape(enum_name)}\s+ADD\s+VALUE"
        r"(?:\s+IF\s+NOT\s+EXISTS)?\s+'(?P<value>[^']+)'",
        re.IGNORECASE,
    )

    for source in sources:
        for match in create_re.finditer(source):
            found.update(re.findall(r"['\"]([^'\"]+)['\"]", match.group("values")))
        for match in alter_re.finditer(source):
            found.add(match.group("value"))

    return found


@pytest.mark.parametrize(
    ("enum_name", "python_enum"),
    [
        ("jobtype", JobType),
        ("jobstatus", JobStatus),
        ("assettype", AssetType),
    ],
)
def test_postgres_enum_covers_python_enum_names(enum_name, python_enum) -> None:
    declared = _values_defined_for(enum_name)
    assert declared, f"no migration declares Postgres enum {enum_name!r}"

    required = {member.name for member in python_enum}
    missing = sorted(required - declared)

    assert not missing, (
        f"Postgres enum {enum_name!r} is missing {missing}. "
        f"SQLAlchemy persists enum NAMES (uppercase), so add "
        f"\"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '<NAME>'\" "
        f"in a new migration for each."
    )
