"""Store user emails case-insensitively.

Reported in production: an account created as ``Mia.Amaranto@evergreengoodwill.org``
could not sign in as ``mia.amaranto@evergreengoodwill.org``; the audit log
recorded ``user_not_found``. ``users.email`` had a plain unique constraint and
every lookup used ``==``, so casing was load-bearing.

This migration backfills existing addresses to lowercase and adds functional
unique indexes on ``lower(email)`` and ``lower(username)``. The application
also normalises on write and case-folds on read, so the indexes are the
belt-and-braces guarantee rather than the only line of defence.

``username`` is included because it is derived from the email local part
(``routers/users.py``), so it inherits the address's casing and would
otherwise let ``Mia.Amaranto`` and ``mia.amaranto`` coexist -- which would in
turn make the case-folded login lookup ambiguous.

Collisions cannot be resolved automatically: two accounts differing only by
case are two distinct sets of group memberships, audit history and assets, and
merging them is a judgement call. The migration therefore aborts with the
offending addresses listed rather than guessing.

Revision ID: 0060
Revises: 0059
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def _abort_on_collisions(conn, column: str) -> None:
    rows = conn.execute(
        sa.text(
            f"""
            SELECT lower({column}) AS folded, count(*) AS n,
                   string_agg({column}, ', ' ORDER BY {column}) AS variants
            FROM users
            GROUP BY lower({column})
            HAVING count(*) > 1
            """
        )
    ).fetchall()
    if not rows:
        return
    detail = "; ".join(f"{r.folded!r} <- {r.variants}" for r in rows)
    raise RuntimeError(
        f"Cannot enforce case-insensitive {column}s: {len(rows)} group(s) of "
        f"users differ only by case ({detail}). These are separate accounts "
        "with separate permissions and history, so this migration will not "
        "merge or rename them automatically. Resolve them by hand (delete or "
        "re-address the duplicates), then re-run the migration."
    )


def upgrade() -> None:
    conn = op.get_bind()

    # SQLite (used by the test suite) has no functional unique indexes in the
    # form below and no string_agg; the ORM-level normalisation is what the
    # tests exercise, so the DB-level guarantee is Postgres-only.
    if conn.dialect.name != "postgresql":
        return

    _abort_on_collisions(conn, "email")
    _abort_on_collisions(conn, "username")

    conn.execute(sa.text("UPDATE users SET email = lower(btrim(email)) "
                         "WHERE email <> lower(btrim(email))"))
    conn.execute(sa.text("UPDATE users SET username = lower(btrim(username)) "
                         "WHERE username <> lower(btrim(username))"))

    op.create_index(
        "uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True
    )
    op.create_index(
        "uq_users_username_lower", "users", [sa.text("lower(username)")], unique=True
    )


def downgrade() -> None:
    # Project policy (tests/test_migration_policy.py): no real downgrades.
    # This one is irreversible in substance anyway -- the original casing is
    # overwritten and not recoverable, and dropping the indexes would not undo
    # that. Lowercased addresses remain valid under the old schema, so there is
    # nothing useful to roll back to.
    raise NotImplementedError("Downgrade is not supported for 0060.")
