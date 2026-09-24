"""Job ORM model — generic queue job for worker container.

A ``Job`` represents one unit of work handed to the worker via the Azure
Storage Queue (in Azure mode) or PostgreSQL NOTIFY (in docker-compose mode).
The queue message body is the job's UUID; the worker looks up the row and
dispatches on ``type``.

The queue is the authority on ownership: visibility timeout + heartbeat
guarantee that at most one worker holds the lease for a given message at a
time.  The row's ``status`` and ``retry_count`` are observability + poison
protection, not a distributed lock.
"""

import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.database import Base


class JobType(str, PyEnum):
    """What kind of work a job represents."""
    VARIANT_TRANSCODE = "variant_transcode"  # target_id → asset_variants.id
    STREAM_CAPTURE = "stream_capture"        # target_id → assets.id (SAVED_STREAM)
    VOICE_SYNTHESIS = "voice_synthesis"      # target_id → assets.id (VOICE_ANNOUNCEMENT)
    # Imager flows (Option E).  Schema/dispatch are wired in PR 2;
    # the actual handlers land in PR 3.  Both target a UUID PK on the
    # corresponding imager table.
    IMAGE_IMPORT = "image_import"            # target_id → base_images.id
    IMAGE_PROVISION = "image_provision"      # target_id → provisioned_images.id


class JobStatus(str, PyEnum):
    """Lifecycle state of a job row."""
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Max retries before a job is considered poison and marked FAILED.
# Retries are reserved for transient failures (network blips, pod crashes
# without SIGTERM).  Replica-timeout (SIGTERM) is handled as a one-shot
# terminal failure by the worker's SIGTERM handler — retrying a transcode
# that already exceeded the time budget is pointless and just burns CPU.
MAX_JOB_RETRIES = 3


class Job(Base):
    __tablename__ = "jobs"

    # At most one live VARIANT_TRANSCODE job per variant.  Two active jobs
    # for one variant means two workers transcoding to the same output blob
    # concurrently — see migration 0066.  Restricted to VARIANT_TRANSCODE on
    # purpose: other job types legitimately re-enqueue the same target
    # (re-synthesising an edited voice announcement, re-importing a failed
    # base image), and a blanket constraint would turn those into 500s.
    __table_args__ = (
        Index(
            "uq_jobs_one_active_per_variant",
            "target_id",
            unique=True,
            postgresql_where=text(
                "type = 'VARIANT_TRANSCODE' "
                "AND status IN ('PENDING', 'PROCESSING')"
            ),
            sqlite_where=text(
                "type = 'VARIANT_TRANSCODE' "
                "AND status IN ('PENDING', 'PROCESSING')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    type: Mapped[JobType] = mapped_column(Enum(JobType), nullable=False, index=True)
    # target_id is NOT a foreign key because it points at different tables
    # depending on ``type`` (asset_variants for VARIANT_TRANSCODE, assets for
    # STREAM_CAPTURE).  Cascade-deletes of the target leave the job row
    # behind as a tombstone — the orphan sweep skips jobs whose targets are
    # gone.
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), nullable=False, default=JobStatus.PENDING, index=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Liveness signal for the worker that currently holds this job.  The
    # worker's heartbeat loop stamps this every HEARTBEAT_INTERVAL seconds
    # for the whole life of the job, in the same DB round-trip it already
    # makes to probe ``cancel_requested`` — so this costs nothing extra.
    #
    # This is the *only* trustworthy liveness signal for a running job.
    # ``AssetVariant.progress`` is not: it is only written when ffmpeg
    # reports a duration (never for livestreams or un-probeable inputs),
    # it stops entirely once the estimate clamps at 99%, and the image /
    # thumbnail / webpage branches jump 0 → 100 with nothing between.
    # Staleness detection must key off this column, not progress or
    # ``created_at`` (which measures total elapsed time, so it declares
    # any legitimately-slow job dead and re-enqueues it forever).
    #
    # NULL means "claimed before this column existed" — treat
    # ``created_at`` as the fallback so a deploy mid-transcode doesn't
    # reap every in-flight job.
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Coarse-grained progress for UI polling.  ``progress_stage`` is a
    # short worker-defined label (e.g. ``downloading``, ``building``,
    # ``uploading``).  ``progress_pct`` is an optional 0-100 estimate;
    # NULL means "unknown / no estimate yet" (distinct from 0%).
    # Used by the imager build + import flows so the UI is not silent
    # for several minutes between PROCESSING and DONE.  Other job
    # types may leave these at their defaults.
    progress_stage: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    progress_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Cooperative cancellation flag.Set by CMS when the target asset is
    # soft-deleted; the worker heartbeat loop reads this and aborts ffmpeg
    # within one heartbeat cycle.  Jobs that have not yet started see it in
    # the pre-transcode guard and no-op.
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class JobOutbox(Base):
    """Transactional outbox row for a Job that owes a queue message.

    Producers INSERT a row here in the same DB transaction as the ``Job``
    INSERT.  A separate drainer task polls this table, calls
    ``queue.send_message`` for each row, then DELETEs the row on success.

    This guarantees that if the Job row exists, either the queue message
    has already been sent OR an outbox row exists that the drainer will
    eventually turn into one.  Replaces the old ``sweep_orphans`` polling
    of ``Job`` rows, which couldn't distinguish "queue msg lost" from
    "workers backlogged" and re-enqueued duplicates.

    Rows that exceed ``MAX_OUTBOX_ATTEMPTS`` are left in place (not
    deleted) so they show up in observability and can be alerted on —
    we never want to silently drop a job.
    """

    __tablename__ = "job_outbox"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Cascade-delete: if the Job row is deleted (e.g. asset hard-deleted),
    # the outbox row goes with it — there's nothing to enqueue.
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")


# Drainer caps an outbox row at this many attempts before giving up and
# leaving it in place for human/operator attention.  20 attempts at the
# capped 60s backoff = ~20 minutes of trying — enough to ride through a
# transient queue outage but short enough to surface real problems.
MAX_OUTBOX_ATTEMPTS = 20
