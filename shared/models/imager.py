"""ORM models for browser-driven Pi image provisioning (Option E).

A two-step pipeline:

1. ``BaseImage`` represents one tenant-cached upstream Pi image
   (e.g. ``pi5 / v1.11.28``).  Imported once by an admin via an
   ``IMAGE_IMPORT`` job: worker downloads from the upstream
   GitHub-Releases catalog, verifies SHA256, uploads to tenant blob.
2. ``ProvisionedImage`` represents one per-build artifact: a
   ``BaseImage`` mutated on the fly to carry a per-fleet ``agora-fleet
   .env`` (CMS URL + device API key).  Produced on demand by an
   ``IMAGE_PROVISION`` job, uploaded to a short-lived blob with a 2 h
   SAS, and auto-deleted by Azure lifecycle policy after 24 h.

This file is the schema/model surface only.  Worker logic lives in
``worker/imager_handlers.py`` (PR 3).  API endpoints live in
``cms/routers/imager.py`` (PR 4).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.database import Base


# ── Status enums (as strings — narrow vocabulary, keeps migrations
# trivial vs. PG enum types we'd have to ALTER).  Defined as Python
# enums for callsite clarity but persisted as plain text.

class BaseImageStatus(str, PyEnum):
    """Lifecycle of a ``BaseImage`` row."""
    IMPORTING = "importing"
    READY = "ready"
    FAILED = "failed"


class ProvisionedImageStatus(str, PyEnum):
    """Lifecycle of a ``ProvisionedImage`` row."""
    PROVISIONING = "provisioning"
    READY = "ready"
    FAILED = "failed"
    # Set by a future cleanup sweep once ``expires_at`` passes and the
    # blob is gone; the row itself is retained for audit.
    EXPIRED = "expired"


class BaseImage(Base):
    """One tenant-cached upstream Pi image.

    Rows are inserted in ``IMPORTING`` state by the catalog-import API
    endpoint, paired with an ``IMAGE_IMPORT`` job whose ``target_id``
    points at this row.  The worker downloads + verifies + uploads,
    then flips ``status`` to ``READY`` and populates ``sha256``,
    ``blob_path``, ``size_bytes``, ``imported_at``.

    The (variant, version) pair is unique — re-importing the same
    version is a no-op rather than a duplicate row.
    """

    __tablename__ = "base_images"
    __table_args__ = (
        UniqueConstraint("variant", "version", name="uq_base_images_variant_version"),
        Index("ix_base_images_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    # 'pi5', 'pi4', 'zero2w' — matches the upstream catalog manifest.
    variant: Mapped[str] = mapped_column(Text, nullable=False)
    # 'v1.11.28' etc. — opaque to the CMS; matched verbatim against
    # the catalog ``ref`` field.
    version: Mapped[str] = mapped_column(Text, nullable=False)
    # Populated by the worker on import success.  Hex-encoded sha256
    # of the on-disk ``.img.xz`` payload, copied from the catalog
    # after verification.
    sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Upstream catalog URL the API resolved at enqueue time.  Immutable
    # for the lifetime of the row -- the worker downloads from this URL
    # rather than re-resolving the (mutable) catalog, eliminating the
    # TOCTOU window between admin click and worker pickup.  Nullable so
    # PR 3 tests can run before PR 4's API populates it.
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Expected SHA256 stamped at enqueue time from the catalog entry.
    # The worker compares against this; on mismatch the import is a
    # terminal failure (tampering signal).  Nullable for the same
    # backfill reason as ``source_url``.
    expected_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Container-relative blob name within
    # ``settings.base_image_cache_container``: ``<variant>/<version>/base.img.xz``.
    # Null until import success.
    blob_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    imported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Audit only — keep the row if the user is later deleted.
    imported_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default=BaseImageStatus.IMPORTING.value,
        server_default=BaseImageStatus.IMPORTING.value,
    )
    error_message: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class ProvisionedImage(Base):
    """One per-build per-fleet provisioned ``.img.xz``.

    Rows are inserted in ``PROVISIONING`` state by the build API
    endpoint, paired with an ``IMAGE_PROVISION`` job whose
    ``target_id`` points at this row.  The worker decrypts
    ``fleet_env_payload``, runs the imager pipeline (PR 1), uploads
    the result, sets ``status=READY`` + ``expires_at = now + 24 h``.

    Output bytes are ephemeral — Azure lifecycle policy on the
    ``provisioned`` container auto-deletes blobs after 24 h.  This
    row is preserved for audit; ``EXPIRED`` is set by a future sweep
    to mark that the blob is no longer downloadable.
    """

    __tablename__ = "provisioned_images"
    __table_args__ = (
        Index("ix_provisioned_images_status", "status"),
        Index("ix_provisioned_images_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    # ``SET NULL`` so admins can delete a cached base image without
    # cascade-destroying the audit trail of past builds. Built
    # ``.img.xz`` artifacts are fully self-contained (the imager
    # pipeline embeds the OS into a fresh blob), so the base image
    # has no runtime relationship to the artifact once a build is
    # done. The denormalized ``base_variant`` / ``base_version``
    # snapshot fields below preserve "which base did this build
    # come from?" after the FK target vanishes.
    base_image_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("base_images.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Audit-only snapshot of the base image's identity at build time.
    # Populated by the build API on insert. Survives base-image
    # deletion (FK above goes to NULL).
    base_variant: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    # FK to the ``IMAGE_PROVISION`` job that produced this row,
    # populated at insert. Lets the list endpoint expose the job_id
    # the existing ``/download/{job_id}`` redirect requires without
    # an extra join. ``SET NULL`` so a job-table cleanup sweep
    # doesn't cascade-destroy audit rows.
    provisioning_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Pi serial / ``String(64)`` — matches ``devices.id``.  Nullable
    # for "preview"/"bulk" builds that aren't bound to a registered
    # device yet.  ``SET NULL`` so device hard-delete leaves the
    # audit row.
    device_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    output_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    blob_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    built_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    built_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Audit-only: the fleet_id the operator selected at build time.
    # Populated by PR 4's API on insert; left null on legacy rows.
    # Useful after terminal success when ``fleet_env_payload`` is cleared.
    fleet_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Plaintext ``agora-fleet.env`` body — UTF-8 bytes of
    # ``AGORA_CMS_URL=...\nAGORA_FLEET_ID=...\nAGORA_FLEET_SECRET=...\n``.
    # Plaintext is intentional: the same secret already lives in clear
    # in the operator's CMS env config (``fleet_register_secrets``) and
    # ends up plaintext on the SD card itself, so encrypting the brief
    # DB copy adds minimal defense-in-depth without a separate key
    # store.  Worker clears this column to NULL on terminal success so
    # the secret does not linger longer than necessary.  Never log
    # this column, never serialize it through the API.
    fleet_env_payload: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True,
    )
    # Caller-supplied output filename.  Validated against
    # ``imager._SAFE_OUTPUT_RE`` before use.
    output_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False,
        default=ProvisionedImageStatus.PROVISIONING.value,
        server_default=ProvisionedImageStatus.PROVISIONING.value,
    )
    error_message: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
