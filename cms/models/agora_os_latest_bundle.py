"""Single-row table holding the latest agora-os bundle metadata.

The row is the **shared source of truth** for "what is the newest non-draft
agora-os release?" across all CMS replicas.  Replaces the per-process
``bundle_checker._latest_bundle`` module global that caused issue #578:
each replica had its own cache, so the UI's ``update_available`` badge
flickered on/off depending on which replica answered each round-robin
request.

The poller in :mod:`cms.services.bundle_checker` UPSERTs into this row
on every successful GitHub fetch (replicated across replicas — writes
are idempotent on identical payloads).  All readers — the device-list
endpoints, the upgrade endpoint, the UI templates — read this row,
which means every replica returns the same view of "latest" at any
given moment.

There is exactly one row, enforced by a ``CHECK (id = 1)`` constraint
in the alembic migration (0026).  The model itself uses ``id`` as the
PK so SQLAlchemy is happy; the CHECK guards against accidentally
INSERTing a second row from buggy code.

Per-replica failure state (``_last_error``) is *not* persisted here —
it lives in :mod:`cms.services.bundle_checker` as a module global on
purpose.  When one replica's poll fails, the others' caches are still
fresh; treating "last error" as per-replica state makes it more useful
for diagnosing partial outages.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from cms.database import Base


class AgoraOsLatestBundle(Base):
    __tablename__ = "agora_os_latest_bundle"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_version: Mapped[str] = mapped_column(Text, nullable=False)
    release_id: Mapped[str] = mapped_column(Text, nullable=False)
    min_from_version: Mapped[str] = mapped_column(Text, nullable=False)
    bundle_url: Mapped[str] = mapped_column(Text, nullable=False)
    signature_url: Mapped[str] = mapped_column(Text, nullable=False)
    sha256_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_success_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
