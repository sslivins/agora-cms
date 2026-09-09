"""Persisted state for the feature-flag system.

Only *state* lives in the database.  What a flag means — its description,
owner, default, kind and expiry — is declared in code in
:mod:`cms.services.feature_flags`, so it travels with the feature it gates,
shows up in review, and cannot drift from the code that reads it.

A missing row is normal rather than an error: a flag with no row falls back to
its registry default.  That keeps a freshly-deployed environment predictable
(nothing has to be seeded for the app to behave correctly) and means deleting a
row degrades to "the declared default" instead of an outage.

``user_ids`` / ``role_ids`` are only consulted in the ``targeted`` state.  They
are stored as JSON lists of UUID strings rather than association tables
because they are read as a whole, never joined against, and are small — a
handful of pilot users per flag.  Membership is evaluated in Python, so no
JSON querying is required and the SQLite test matrix needs no special support.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from cms.database import Base

# JSONB on Postgres, generic JSON on SQLite (test matrix).
_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class FeatureFlagState(Base):
    """Stored state for one registry-declared feature flag."""

    __tablename__ = "feature_flags"

    # The registry key.  Not a surrogate id: the name *is* the identity, it is
    # what code passes to ``enabled()``, and it makes the table readable when
    # inspected directly during an incident.
    name: Mapped[str] = mapped_column(String(100), primary_key=True)

    # "off" | "targeted" | "on" — see cms.services.feature_flags.FlagState.
    # Stored as text rather than a DB enum so adding a state later doesn't
    # require a migration on a table this small.
    state: Mapped[str] = mapped_column(String(16), nullable=False)

    user_ids: Mapped[list] = mapped_column(
        _JSON_TYPE, nullable=False, default=list, server_default="[]"
    )
    role_ids: Mapped[list] = mapped_column(
        _JSON_TYPE, nullable=False, default=list, server_default="[]"
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    # SET NULL rather than CASCADE: deleting the admin who flipped a flag must
    # not delete the flag along with them.
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
