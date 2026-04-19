"""Dialect-portable JSON key-extraction helpers.

SQLAlchemy's Postgres ``JSONB.Comparator`` exposes ``.astext`` which
compiles to ``col ->> 'key'``.  On SQLite (used in the test suite),
``details[key]`` yields a generic JSON comparator that has no
``astext`` attribute, so any query that uses it raises
``AttributeError`` at build time.

``json_as_text(col, key)`` returns an expression that compiles to:

* PostgreSQL: ``col ->> 'key'`` (identical to ``.astext``)
* SQLite:     ``json_extract(col, '$.key')`` (returns unquoted TEXT)
* Other:      best-effort ``json_extract`` fallback (same as SQLite)

Use it anywhere we need to read a string out of a ``JSON``/``JSONB``
column in a query that should run on both Postgres (prod) and SQLite
(tests).
"""
from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import expression


class _JsonAsText(expression.FunctionElement):
    """SQL expression node representing ``col ->> key`` as portable TEXT."""

    type = String()
    inherit_cache = True
    name = "json_as_text"


@compiles(_JsonAsText, "postgresql")
def _json_as_text_pg(element, compiler, **kw):  # pragma: no cover - exercised in prod
    col, key = list(element.clauses)
    return "(%s ->> %s)" % (compiler.process(col, **kw), compiler.process(key, **kw))


@compiles(_JsonAsText, "sqlite")
def _json_as_text_sqlite(element, compiler, **kw):
    col, key = list(element.clauses)
    # json_extract with a string key returns an unquoted TEXT value for
    # string-typed JSON leaves — which matches Postgres's ->> behaviour.
    return "json_extract(%s, '$.' || %s)" % (
        compiler.process(col, **kw),
        compiler.process(key, **kw),
    )


@compiles(_JsonAsText)
def _json_as_text_default(element, compiler, **kw):
    # Fallback: same as SQLite; most modern RDBMSes have json_extract.
    col, key = list(element.clauses)
    return "json_extract(%s, '$.' || %s)" % (
        compiler.process(col, **kw),
        compiler.process(key, **kw),
    )


def json_as_text(col, key: str):
    """Portable ``col ->> 'key'`` returning TEXT on Postgres and SQLite."""
    return _JsonAsText(col, expression.literal(key))
