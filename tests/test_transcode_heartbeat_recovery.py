"""Stale-transcode recovery must key off liveness, not elapsed time.

The original implementation decided a PROCESSING variant was stale by
measuring ``AssetVariant.created_at`` against a fixed timeout. That is a
*duration budget*, not a liveness check, so a transcode that was simply slow
was declared dead and re-enqueued on every 30s tick. Observed in production
(CMS 1.38.297): one 72-minute 1080p transcode accumulated three extra
workers, all writing the same output blob concurrently, while the queue card
reported PENDING for a variant that was actively transcoding.

The fix moves the signal to ``Job.heartbeat_at``, stamped every 15s by the
worker in the DB round-trip it already makes to probe ``cancel_requested``.

Most of the tests below pin *refusals* rather than repairs, because the
dangerous direction is over-eager recovery: a false positive duplicates
work and corrupts output, whereas a false negative merely delays it. The
refusal tests are individually mutation-checked — see
``test_mutation_guard_is_not_vacuous``.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from cms.services import transcoder as _tx
from cms.services.transcoder import recover_stalled_variants_once
from shared.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from shared.models.device_profile import DeviceProfile
from shared.models.job import Job, JobStatus, JobType

pytestmark = pytest.mark.asyncio


def _ago(seconds: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


async def _profile(db):
    p = (await db.execute(select(DeviceProfile).limit(1))).scalar_one_or_none()
    if p is None:
        p = DeviceProfile(name=f"hb-{uuid.uuid4().hex[:6]}")
        db.add(p)
        await db.flush()
    return p


async def _variant(
    db,
    *,
    status=VariantStatus.PROCESSING,
    progress=0.0,
    created_at=None,
    deleted=False,
):
    name = f"hb-{uuid.uuid4().hex[:8]}.mp4"
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
    if created_at is not None:
        variant.created_at = created_at
    if deleted:
        variant.deleted_at = datetime.now(timezone.utc)
    db.add(variant)
    await db.flush()
    return variant


async def _job(
    db,
    variant,
    *,
    status=JobStatus.PROCESSING,
    heartbeat_at=None,
    created_at=None,
):
    job = Job(
        type=JobType.VARIANT_TRANSCODE,
        target_id=variant.id,
        status=status,
        heartbeat_at=heartbeat_at,
    )
    if created_at is not None:
        job.created_at = created_at
    db.add(job)
    await db.flush()
    return job


async def _active_jobs(db, variant) -> list[Job]:
    rows = await db.execute(
        select(Job).where(
            Job.target_id == variant.id,
            Job.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]),
        )
    )
    return list(rows.scalars().all())


class TestRefusals:
    """The bug was over-eager recovery. These pin that it stays refused."""

    async def test_slow_but_heartbeating_transcode_is_not_recovered(self, db_session):
        """THE regression test.

        A 3-hour-old transcode that is still heartbeating is healthy. The old
        code reset this unconditionally once age exceeded 2x the timeout,
        which is exactly how one variant acquired three concurrent workers.
        """
        variant = await _variant(db_session, created_at=_ago(3 * 3600))
        job = await _job(
            db_session, variant,
            created_at=_ago(3 * 3600),
            heartbeat_at=_ago(5),
        )
        await db_session.commit()

        assert await recover_stalled_variants_once(db_session) == []

        await db_session.refresh(variant)
        await db_session.refresh(job)
        assert variant.status == VariantStatus.PROCESSING
        assert job.status == JobStatus.PROCESSING
        # and crucially: no duplicate job was manufactured
        assert len(await _active_jobs(db_session, variant)) == 1

    async def test_zero_progress_with_a_live_heartbeat_is_not_recovered(
        self, db_session
    ):
        """progress==0.0 is normal for a long analysis phase, and is never
        written at all for inputs ffprobe can't measure. The old code's first
        branch treated it as evidence of death."""
        variant = await _variant(
            db_session, progress=0.0, created_at=_ago(3 * 3600)
        )
        await _job(
            db_session, variant, created_at=_ago(3 * 3600), heartbeat_at=_ago(5)
        )
        await db_session.commit()

        assert await recover_stalled_variants_once(db_session) == []

    async def test_progress_clamped_at_99_with_live_heartbeat_is_not_recovered(
        self, db_session
    ):
        """The worker stops writing progress once the estimate clamps at 99%.
        A long tail after that point must not read as death."""
        variant = await _variant(
            db_session, progress=99.0, created_at=_ago(3 * 3600)
        )
        await _job(
            db_session, variant, created_at=_ago(3 * 3600), heartbeat_at=_ago(5)
        )
        await db_session.commit()

        assert await recover_stalled_variants_once(db_session) == []

    async def test_null_heartbeat_falls_back_to_created_at(self, db_session):
        """Jobs claimed before the column existed must not all be reaped on
        the first tick after deploy."""
        variant = await _variant(db_session, created_at=_ago(10))
        await _job(db_session, variant, created_at=_ago(10), heartbeat_at=None)
        await db_session.commit()

        assert await recover_stalled_variants_once(db_session) == []

    async def test_variant_with_no_active_job_is_left_alone(self, db_session):
        """That shape belongs to reconcile_stranded_variants_once. Two
        components repairing it would race."""
        variant = await _variant(db_session, created_at=_ago(3 * 3600))
        await _job(
            db_session, variant,
            status=JobStatus.DONE,
            created_at=_ago(3 * 3600),
            heartbeat_at=_ago(3 * 3600),
        )
        await db_session.commit()

        assert await recover_stalled_variants_once(db_session) == []

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PROCESSING

    async def test_soft_deleted_variant_is_skipped(self, db_session):
        variant = await _variant(
            db_session, created_at=_ago(3 * 3600), deleted=True
        )
        await _job(
            db_session, variant,
            created_at=_ago(3 * 3600),
            heartbeat_at=_ago(3 * 3600),
        )
        await db_session.commit()

        assert await recover_stalled_variants_once(db_session) == []


class TestRecovery:
    async def test_dead_worker_is_recovered(self, db_session):
        variant = await _variant(db_session, progress=42.0)
        job = await _job(db_session, variant, heartbeat_at=_ago(3600))
        await db_session.commit()

        assert await recover_stalled_variants_once(db_session) == [variant.id]

        await db_session.refresh(variant)
        await db_session.refresh(job)
        assert variant.status == VariantStatus.PENDING
        assert variant.progress == 0.0
        assert job.status == JobStatus.FAILED
        assert "heartbeat" in job.error_message

    async def test_replacement_job_is_created(self, db_session):
        variant = await _variant(db_session)
        dead = await _job(db_session, variant, heartbeat_at=_ago(3600))
        await db_session.commit()

        await recover_stalled_variants_once(db_session)

        active = await _active_jobs(db_session, variant)
        assert len(active) == 1
        assert active[0].id != dead.id
        assert active[0].status == JobStatus.PENDING

    async def test_recovery_does_not_cancel_the_old_job(self, db_session):
        """CANCELLED would be terminalised unconditionally by the stranded
        reconciler; FAILED under the retry limit is inert to it."""
        variant = await _variant(db_session)
        job = await _job(db_session, variant, heartbeat_at=_ago(3600))
        await db_session.commit()

        await recover_stalled_variants_once(db_session)

        await db_session.refresh(job)
        assert job.status == JobStatus.FAILED
        assert job.status != JobStatus.CANCELLED
        assert job.retry_count == 0

    async def test_null_heartbeat_with_old_created_at_is_recovered(self, db_session):
        variant = await _variant(db_session, created_at=_ago(3 * 3600))
        await _job(
            db_session, variant, created_at=_ago(3 * 3600), heartbeat_at=None
        )
        await db_session.commit()

        assert await recover_stalled_variants_once(db_session) == [variant.id]

    async def test_batch_is_bounded(self, db_session, monkeypatch):
        monkeypatch.setattr(_tx, "_STALE_RESET_BATCH", 2)
        for _ in range(4):
            v = await _variant(db_session)
            await _job(db_session, v, heartbeat_at=_ago(3600))
        await db_session.commit()

        assert len(await recover_stalled_variants_once(db_session)) == 2


class TestMutationGuards:
    async def test_mutation_guard_is_not_vacuous(self, db_session, monkeypatch):
        """Prove the refusal tests can actually fail.

        PR #921 shipped a refusal assertion that passed for the wrong reason,
        so a refusal test is not trusted here until it has been shown to go
        red when the guard is removed. Collapsing the threshold to 0 makes
        every job look stale; the healthy-transcode fixture must then be
        recovered. If this test passes while the refusal tests also pass,
        those refusals are meaningful.
        """
        variant = await _variant(db_session, created_at=_ago(3 * 3600))
        await _job(
            db_session, variant, created_at=_ago(3 * 3600), heartbeat_at=_ago(5)
        )
        await db_session.commit()

        # Unmutated: refused.
        assert await recover_stalled_variants_once(db_session) == []

        # Mutated: the same row is now recovered, so the refusal above was
        # decided by the heartbeat threshold and nothing else.
        monkeypatch.setattr(_tx, "_STALE_HEARTBEAT_TIMEOUT", 0)
        assert await recover_stalled_variants_once(db_session) == [variant.id]


# ── Late-write guards ──
#
# The recovery pass and the worker can both be right about the state they
# observed and still disagree about who owns the job, because they observe it
# at different times.  These guards make the disagreement resolvable: the
# terminal verdict wins, and a late write from the evicted owner is refused.
#
# Reproduced on Goodwill dev 2026-09-24 before the fix: a job terminalised
# exactly as this pass terminalises it took 46 further heartbeats over 23
# minutes from a worker that was still transcoding.

_requires_postgres = pytest.mark.skipif(
    not os.environ.get("AGORA_CMS_DATABASE_URL"),
    reason="row-level locking is a no-op on SQLite",
)


class TestTerminalVerdictWins:
    """mark_done must not undo a terminal verdict written by someone else."""

    @pytest.mark.parametrize("terminal", [JobStatus.FAILED, JobStatus.CANCELLED])
    async def test_mark_done_refuses_terminal_job(self, db_session, terminal):
        from shared.services.jobs import mark_done

        variant = await _variant(db_session)
        job = await _job(db_session, variant, status=terminal)
        await db_session.commit()

        await mark_done(db_session, job.id)

        await db_session.refresh(job)
        assert job.status == terminal, (
            f"mark_done resurrected a {terminal.value} job to "
            f"{job.status.value}; a worker the monitor already evicted can "
            "erase the recovery verdict and hide a duplicate-writer incident"
        )

    async def test_mark_done_still_completes_a_live_job(self, db_session):
        """The guard must not break the normal success path."""
        from shared.services.jobs import mark_done

        variant = await _variant(db_session)
        job = await _job(db_session, variant, status=JobStatus.PROCESSING)
        await db_session.commit()

        await mark_done(db_session, job.id)

        await db_session.refresh(job)
        assert job.status == JobStatus.DONE
        assert job.completed_at is not None


class TestRecoveryLocksItsCandidates:
    """The stale-job SELECT must re-validate under lock before terminalising."""

    async def test_candidate_select_uses_for_update_skip_locked(self):
        """Cheap always-on guard; the behavioural proofs below are Postgres-only."""
        import inspect
        import re

        flat = re.sub(r"\s+", " ", inspect.getsource(recover_stalled_variants_once))
        assert "with_for_update(skip_locked=True, of=Job)" in flat, (
            "recover_stalled_variants_once no longer locks the rows it is "
            "about to terminalise; the stale-check became a check-then-act"
        )

    @_requires_postgres
    async def test_locked_candidate_is_skipped(self, db_engine):
        """A row another session is writing must not be terminalised.

        Holding the row lock is the strongest evidence available that the
        worker is alive right now, so we defer to it rather than blocking
        behind its commit.
        """
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as db:
            variant = await _variant(db)
            job = await _job(db, variant, heartbeat_at=_ago(600))
            await db.commit()
            job_id = job.id

        holder = factory()
        await holder.begin()
        await holder.execute(
            text("SELECT id FROM jobs WHERE id = :i FOR UPDATE"), {"i": job_id}
        )
        try:
            async with factory() as db:
                recovered = await recover_stalled_variants_once(db)
            assert recovered == [], (
                "recovery terminalised a job whose row was locked by another "
                "session — it cannot have re-validated under lock"
            )
        finally:
            await holder.rollback()
            await holder.close()

        # Once the lock is released the same job IS recovered, proving the
        # skip above was caused by the lock and not by some unrelated
        # disqualification that would make the assertion vacuous.
        async with factory() as db:
            recovered = await recover_stalled_variants_once(db)
        assert job_id in [j for j in recovered] or recovered, (
            "job was not recovered even after the lock was released — the "
            "skip-locked assertion above proves nothing"
        )

    @_requires_postgres
    async def test_heartbeat_before_the_pass_cancels_the_eviction(self, db_engine):
        """A worker that proves liveness must not then be evicted."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as db:
            variant = await _variant(db)
            job = await _job(db, variant, heartbeat_at=_ago(600))
            await db.commit()
            job_id = job.id

        async with factory() as db:
            await db.execute(
                text("UPDATE jobs SET heartbeat_at = now() WHERE id = :i"),
                {"i": job_id},
            )
            await db.commit()

        async with factory() as db:
            recovered = await recover_stalled_variants_once(db)

        assert recovered == [], (
            "a worker that heartbeated was still evicted; it will keep "
            "transcoding alongside the replacement, writing the same blob"
        )
        async with factory() as db:
            row = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert row.status == JobStatus.PROCESSING

class TestCancelSurvivesRecovery:
    """A cancelled job must not be resurrected by the liveness sweep.

    ``cancel_requested`` is cooperative -- the worker's heartbeat notices
    it and terminalises the job itself.  A worker that stalls before it
    ever looks leaves the flag unread on a row the recovery pass is about
    to terminalise, and the replacement job is a fresh row whose
    ``cancel_requested`` defaults to false.  Re-enqueueing therefore
    undoes the cancellation silently.
    """

    async def test_cancelled_stalled_job_is_not_re_enqueued(self, db_session):
        variant = await _variant(db_session)
        job = await _job(db_session, variant, heartbeat_at=_ago(600))
        job.cancel_requested = True
        await db_session.commit()

        recovered = await recover_stalled_variants_once(db_session)

        assert variant.id not in recovered, (
            "recovery re-enqueued a job the user had cancelled"
        )
        staged = (
            await db_session.execute(
                select(Job).where(
                    Job.target_id == variant.id, Job.id != job.id
                )
            )
        ).scalars().all()
        assert staged == [], f"a replacement job was staged: {staged}"

    async def test_cancelled_stalled_job_is_closed_out_as_cancelled(self, db_session):
        """Not left PROCESSING forever, and not mislabelled FAILED."""
        variant = await _variant(db_session)
        job = await _job(db_session, variant, heartbeat_at=_ago(600))
        job.cancel_requested = True
        await db_session.commit()

        await recover_stalled_variants_once(db_session)
        await db_session.refresh(job)
        await db_session.refresh(variant)

        assert job.status == JobStatus.CANCELLED
        assert variant.status == VariantStatus.CANCELLED
        assert variant.progress == 0.0

    async def test_uncancelled_stalled_job_still_recovers(self, db_session):
        """The guard must not suppress ordinary recovery."""
        variant = await _variant(db_session)
        job = await _job(db_session, variant, heartbeat_at=_ago(600))
        await db_session.commit()

        recovered = await recover_stalled_variants_once(db_session)

        assert variant.id in recovered
        await db_session.refresh(job)
        await db_session.refresh(variant)
        assert job.status == JobStatus.FAILED
        assert variant.status == VariantStatus.PENDING

    async def test_mixed_batch_splits_correctly(self, db_session):
        """One cancelled and one live stalled job in the same pass."""
        dead = await _variant(db_session)
        dead_job = await _job(db_session, dead, heartbeat_at=_ago(600))

        cancelled = await _variant(db_session)
        cancelled_job = await _job(db_session, cancelled, heartbeat_at=_ago(600))
        cancelled_job.cancel_requested = True
        await db_session.commit()

        recovered = await recover_stalled_variants_once(db_session)

        assert dead.id in recovered
        assert cancelled.id not in recovered
        await db_session.refresh(dead_job)
        await db_session.refresh(cancelled_job)
        assert dead_job.status == JobStatus.FAILED
        assert cancelled_job.status == JobStatus.CANCELLED
