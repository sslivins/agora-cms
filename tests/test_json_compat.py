"""Unit tests for the dialect-portable ``json_as_text`` helper.

The helper replaces PostgreSQL-only ``JSONB.Comparator.astext`` so queries
that extract a string value out of a JSON(B) column compile cleanly on
both production (Postgres) and the test suite (SQLite).
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from cms.services.json_compat import json_as_text


# CI test image doesn't ship psycopg2 (runtime uses asyncpg). Skip tests
# that need to build a Postgres-dialect engine when it's not installed —
# the SQLite variants still run and give us the portability regression
# guard we care about.
try:  # pragma: no cover — trivial import probe
    import psycopg2  # noqa: F401
    _HAS_PSYCOPG2 = True
except ImportError:
    _HAS_PSYCOPG2 = False

_requires_psycopg2 = pytest.mark.skipif(
    not _HAS_PSYCOPG2,
    reason="psycopg2 not installed; skipping Postgres dialect compile tests",
)


class _Base(DeclarativeBase):
    pass


class _Row(_Base):
    __tablename__ = "rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    details: Mapped[dict] = mapped_column(JSONB)


def _compile(dialect_url: str, stmt) -> str:
    eng = create_engine(dialect_url)
    return str(
        stmt.compile(
            dialect=eng.dialect, compile_kwargs={"literal_binds": True}
        )
    )


@_requires_psycopg2
def test_compiles_to_arrow_arrow_on_postgres():
    stmt = select(json_as_text(_Row.details, "actor_username"))
    sql = _compile("postgresql://", stmt)
    # Postgres idiom: col ->> 'key'
    assert "->>" in sql
    assert "actor_username" in sql
    # Must not leak sqlite's json_extract
    assert "json_extract" not in sql.lower()


def test_compiles_to_json_extract_on_sqlite():
    stmt = select(json_as_text(_Row.details, "actor_username"))
    sql = _compile("sqlite:///:memory:", stmt)
    # SQLite idiom: json_extract(col, '$.key')
    assert "json_extract" in sql.lower()
    assert "actor_username" in sql
    # Must not leak postgres operator
    assert "->>" not in sql


@_requires_psycopg2
def test_usable_in_where_and_distinct():
    """Regression guard — the original AttributeError fired when building
    these clauses, never mind compiling them."""
    expr = json_as_text(_Row.details, "actor_username")
    # These must all be legal at query-build time on every dialect.
    stmt = (
        select(expr)
        .distinct()
        .where(expr.isnot(None))
        .where(expr != "")
        .where(expr == "alice")
    )
    # And compile on both dialects.
    _compile("postgresql://", stmt)
    _compile("sqlite:///:memory:", stmt)
