"""Channel-keyed table holding the latest agora-os bundle per release channel.

Supersedes the single-row ``agora_os_latest_bundle`` table (migration 0026),
which tracked exactly one "newest non-draft release" for the whole fleet.
That model made it impossible to test a build in production without shipping
it to every device: the CMS always offered the newest non-draft release —
prereleases included — to everyone.

This table stores **one row per release channel** (PK = ``channel``):

* ``stable``      — the newest non-draft release that is NOT flagged as a
                    GitHub prerelease.  Devices are on this channel by default.
* ``prerelease``  — the newest non-draft release, prereleases included
                    (i.e. the old single-row behaviour).  A device only lands
                    here if an operator opts it in via ``devices.update_channel``.

The signal that separates the two is GitHub's per-release ``prerelease``
boolean.  agora-os ``release.yml`` sets it automatically from the tag: a
plain ``vX.Y.Z`` tag publishes a stable release, while any pre-release
suffix (``vX.Y.Z-test`` / ``-rc.N``) publishes a GitHub prerelease.

The poller in :mod:`cms.services.bundle_checker` UPSERTs both rows on every
successful GitHub fetch (replicated across replicas — writes are idempotent
on identical payloads).  All readers take a ``channel`` argument and read the
matching row, so every replica returns the same view of "latest for channel X"
at any given moment (the cross-replica invariant from issue #578 is preserved,
now per channel).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from cms.database import Base

# Canonical channel identifiers. Kept here (a leaf model module that only
# imports Base) so both the service layer and the Device model can reference
# them without creating an import cycle.
CHANNEL_STABLE = "stable"
CHANNEL_PRERELEASE = "prerelease"
CHANNELS = (CHANNEL_STABLE, CHANNEL_PRERELEASE)


class AgoraOsChannelBundle(Base):
    __tablename__ = "agora_os_channel_bundle"

    channel: Mapped[str] = mapped_column(Text, primary_key=True)
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
