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

from sqlalchemy import DateTime, Enum, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.database import Base


class JobType(str, PyEnum):
    """What kind of work a job represents."""
    VARIANT_TRANSCODE = "variant_transcode"  # target_id → asset_variants.id
    STREAM_CAPTURE = "stream_capture"        # target_id → assets.id (SAVED_STREAM)


class JobStatus(str, PyEnum):
    """Lifecycle state of a job row."""
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


# Max retries before a job is considered poison and marked FAILED.
MAX_JOB_RETRIES = 5


class Job(Base):
    __tablename__ = "jobs"

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
