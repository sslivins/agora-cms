"""At most one live VARIANT_TRANSCODE job per variant.

Nothing ever enforced this. The stale monitor manufactured replacements for
transcodes that were merely slow (see ``test_transcode_heartbeat_recovery``),
each replacement was claimed by its own worker, and several workers then
transcoded one variant to the same output blob concurrently. Removing the
source of the duplicates is necessary but not sufficient: any future caller
that re-enqueues an in-flight variant reopens the same hole silently.

Two layers are pinned here:

* ``stage_jobs`` skips a target that already has an active job of the same
  type, so the common case never depends on an exception;
* the partial unique index ``uq_jobs_one_active_per_variant`` rejects the
  insert outright, so a caller that bypasses the check — or two replicas
  racing between the SELECT and the INSERT — fails loudly instead of
  corrupting output.

The index is deliberately scoped to VARIANT_TRANSCODE: voice re-synthesis,
base-image re-import and stream re-capture all legitimately re-enqueue the
same target, and a blanket constraint would turn those into 500s. The
type-scoping tests below are what keep someone from "simplifying" it.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from cms.services.transcoder import enqueue_variants, recover_stalled_variants_once
from shared.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from shared.models.device_profile import DeviceProfile
from shared.models.job import Job, JobOutbox, JobStatus, JobType
from shared.services.jobs import stage_jobs

pytestmark = pytest.mark.asyncio


def _ago(seconds: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


async def _profile(db):
    p = (await db.execute(select(DeviceProfile).limit(1))).scalar_one_or_none()
    if p is None:
        p = DeviceProfile(name=f"uq-{uuid.uuid4().hex[:6]}")
        db.add(p)
        await db.flush()
    return p


async def _variant(db, *, status=VariantStatus.PROCESSING, progress=0.0):
    name = f"uq-{uuid.uuid4().hex[:8]}.mp4"
    asset = Asset(
        filename=name, asset_type=AssetType.VIDEO, size_bytes=1, checksum=name
    )
    db.add(asset)
    await db.flush()
    variant = AssetVariant(
        source_asset_id=asset.id,
        profile_id=(await _profile(db)).id,
        filename=f"v-{name}",
        status=status,
        progress=progress,
    )
    db.add(variant)
    await db.flush()
    return variant


async def _job(db, target_id, *, status=JobStatus.PROCESSING,
               jtype=JobType.VARIANT_TRANSCODE, heartbeat_at=None):
    job = Job(
        type=jtype, target_id=target_id, status=status, heartbeat_at=heartbeat_at
    )
    db.add(job)
    await db.flush()
    return job


async def _active_jobs(db, target_id, jtype=JobType.VARIANT_TRANSCODE) -> list[Job]:
    rows = await db.execute(
        select(Job).where(
            Job.target_id == target_id,
            Job.type == jtype,
            Job.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]),
        )
    )
    return list(rows.scalars().all())


class TestStageJobsDedupe:
    """The application-level guard: skip, don't duplicate."""

    @pytest.mark.parametrize("existing", [JobStatus.PENDING, JobStatus.PROCESSING])
    async def test_skips_target_with_active_job(self, db_session, existing):
        variant = await _variant(db_session)
        await _job(db_session, variant.id, status=existing)

        staged = await stage_jobs(
            db_session, [(JobType.VARIANT_TRANSCODE, variant.id)]
        )

        assert staged == [], (
            f"staged a second job for a variant whose job is {existing.name}"
        )
        assert len(await _active_jobs(db_session, variant.id)) == 1

    @pytest.mark.parametrize(
        "existing",
        [JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED],
    )
    async def test_does_not_skip_when_prior_job_is_terminal(self, db_session, existing):
        """Retry must still work. Over-blocking strands the variant forever."""
        variant = await _variant(db_session)
        await _job(db_session, variant.id, status=existing)

        staged = await stage_jobs(
            db_session, [(JobType.VARIANT_TRANSCODE, variant.id)]
        )

        assert len(staged) == 1, (
            f"refused to re-enqueue after a {existing.name} job — retries are dead"
        )

    async def test_does_not_skip_a_different_job_type_for_same_target(self, db_session):
        """Dedupe is per (type, target), not per target."""
        target = uuid.uuid4()
        await _job(db_session, target, jtype=JobType.STREAM_CAPTURE)

        staged = await stage_jobs(db_session, [(JobType.VARIANT_TRANSCODE, target)])

        assert len(staged) == 1

    async def test_stages_outbox_row_for_each_job(self, db_session):
        """A Job without its outbox row is never delivered to a worker."""
        variant = await _variant(db_session)
        staged = await stage_jobs(
            db_session, [(JobType.VARIANT_TRANSCODE, variant.id)]
        )
        await db_session.flush()

        rows = await db_session.execute(
            select(JobOutbox).where(JobOutbox.job_id == staged[0].id)
        )
        assert rows.scalar_one_or_none() is not None

    async def test_does_not_commit(self, db_session):
        """Callers compose stage_jobs with other writes in one transaction."""
        variant = await _variant(db_session)
        await stage_jobs(db_session, [(JobType.VARIANT_TRANSCODE, variant.id)])

        await db_session.rollback()

        assert await _active_jobs(db_session, variant.id) == []

    async def test_deduplicates_within_a_single_batch(self, db_session):
        """The same variant twice in one call is still one job."""
        variant = await _variant(db_session)

        staged = await stage_jobs(
            db_session,
            [
                (JobType.VARIANT_TRANSCODE, variant.id),
                (JobType.VARIANT_TRANSCODE, variant.id),
            ],
        )

        assert len(staged) == 1, "a duplicated spec produced two live jobs"

    async def test_enqueue_variants_skips_active_variant(self, db_session):
        """The CMS-facing wrapper inherits the guard."""
        busy = await _variant(db_session)
        idle = await _variant(db_session)
        await _job(db_session, busy.id, status=JobStatus.PROCESSING)
        await db_session.commit()

        job_ids = await enqueue_variants(db_session, [busy.id, idle.id])

        assert len(job_ids) == 1
        assert len(await _active_jobs(db_session, busy.id)) == 1
        assert len(await _active_jobs(db_session, idle.id)) == 1


class TestDatabaseConstraint:
    """The backstop: the DB refuses the row even if the guard is bypassed."""

    @pytest.mark.parametrize("second", [JobStatus.PENDING, JobStatus.PROCESSING])
    async def test_second_active_transcode_job_is_rejected(self, db_session, second):
        variant = await _variant(db_session)
        await _job(db_session, variant.id, status=JobStatus.PROCESSING)
        await db_session.commit()

        db_session.add(
            Job(
                type=JobType.VARIANT_TRANSCODE,
                target_id=variant.id,
                status=second,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_terminal_jobs_do_not_occupy_the_slot(self, db_session):
        """Unbounded job history for one variant must remain legal."""
        variant = await _variant(db_session)
        for status in (JobStatus.FAILED, JobStatus.DONE, JobStatus.CANCELLED,
                       JobStatus.FAILED):
            await _job(db_session, variant.id, status=status)
        await _job(db_session, variant.id, status=JobStatus.PENDING)
        await db_session.commit()

        assert len(await _active_jobs(db_session, variant.id)) == 1

    async def test_other_job_types_may_have_two_active_jobs(self, db_session):
        """Voice re-synthesis / re-import / re-capture must not become 500s."""
        target = uuid.uuid4()
        await _job(db_session, target, jtype=JobType.STREAM_CAPTURE,
                   status=JobStatus.PROCESSING)
        await _job(db_session, target, jtype=JobType.STREAM_CAPTURE,
                   status=JobStatus.PENDING)

        await db_session.commit()  # must not raise

        assert len(
            await _active_jobs(db_session, target, JobType.STREAM_CAPTURE)
        ) == 2

    async def test_distinct_variants_are_unaffected(self, db_session):
        a = await _variant(db_session)
        b = await _variant(db_session)
        await _job(db_session, a.id, status=JobStatus.PROCESSING)
        await _job(db_session, b.id, status=JobStatus.PROCESSING)

        await db_session.commit()  # must not raise


class TestRecoveryAtomicity:
    """Recovery replaces a job; it must never be observable as 0 or 2."""

    async def test_replacement_job_is_created(self, db_session):
        """Recovery must hand the variant off, not just kill its job.

        Note: this does not isolate the explicit ``flush()`` before
        ``stage_jobs`` — autoflush covers it today, and removing the flush
        leaves these tests green. The flush is there so the handoff does not
        depend on autoflush staying enabled; see the comment at the call
        site.
        """
        variant = await _variant(db_session)
        dead = await _job(db_session, variant.id, heartbeat_at=_ago(3600))
        await db_session.commit()

        recovered = await recover_stalled_variants_once(db_session)

        assert recovered == [variant.id]
        await db_session.refresh(dead)
        assert dead.status == JobStatus.FAILED
        active = await _active_jobs(db_session, variant.id)
        assert len(active) == 1, (
            "recovery left the variant with "
            f"{len(active)} active jobs instead of exactly one"
        )
        assert active[0].id != dead.id
        assert active[0].status == JobStatus.PENDING

    async def test_recovered_variant_is_reset_to_pending(self, db_session):
        variant = await _variant(db_session, progress=42.0)
        await _job(db_session, variant.id, heartbeat_at=_ago(3600))
        await db_session.commit()

        await recover_stalled_variants_once(db_session)

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PENDING
        assert variant.progress == 0.0

    async def test_dead_job_retry_count_is_untouched(self, db_session):
        """The stranded-variant reconciler only acts on FAILED once retries
        are exhausted; bumping retry_count here would make it act."""
        variant = await _variant(db_session)
        dead = await _job(db_session, variant.id, heartbeat_at=_ago(3600))
        before = dead.retry_count
        await db_session.commit()

        await recover_stalled_variants_once(db_session)

        await db_session.refresh(dead)
        assert dead.retry_count == before

    async def test_recovery_of_many_variants_is_one_job_each(self, db_session):
        variants = [await _variant(db_session) for _ in range(3)]
        for v in variants:
            await _job(db_session, v.id, heartbeat_at=_ago(3600))
        await db_session.commit()

        recovered = await recover_stalled_variants_once(db_session)

        assert set(recovered) == {v.id for v in variants}
        for v in variants:
            assert len(await _active_jobs(db_session, v.id)) == 1
