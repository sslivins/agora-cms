"""Variant status must not be stranded when its job has finished.

Variant status and job status are written by different processes, so they
drift. The pre-transcode cancel path marked only the job, leaving the
variant PENDING forever: the supersession sweep only soft-deletes variants
that are READY/FAILED/CANCELLED, so a PENDING one is never swept, and when
the asset is still alive (a profile edit rather than a delete) nothing else
ever revisits it. Each such edit left a permanent phantom row in the
Transcoding Queue card.

The reconciliation sweep is the backstop for rows already stranded and for
crash paths. It is deliberately narrow, and most of these tests exist to
pin the narrowness rather than the happy path -- the cheap version of this
sweep is unsafe:

  * a variant can have several jobs, so "a terminal job exists" is the
    wrong predicate;
  * a FAILED job under the retry limit can still be redelivered;
  * DONE must never be inferred into READY.
"""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cms.services.transcoder import reconcile_stranded_variants_once
from shared.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from shared.models.device_profile import DeviceProfile
from shared.models.job import Job, JobStatus, JobType, MAX_JOB_RETRIES
from worker.transcoder import mark_variant_failed_on_exhaustion

pytestmark = pytest.mark.asyncio


async def _profile(db):
    from sqlalchemy import select
    p = (await db.execute(select(DeviceProfile).limit(1))).scalar_one_or_none()
    if p is None:
        p = DeviceProfile(name=f"recon-{uuid.uuid4().hex[:6]}")
        db.add(p)
        await db.flush()
    return p


async def _variant(db, status=VariantStatus.PENDING, asset_deleted=False):
    name = f"recon-{uuid.uuid4().hex[:8]}.mp4"
    asset = Asset(
        filename=name,
        asset_type=AssetType.VIDEO,
        size_bytes=1,
        checksum=name,
    )
    if asset_deleted:
        from datetime import datetime, timezone
        asset.deleted_at = datetime.now(timezone.utc)
    db.add(asset)
    await db.flush()

    profile = await _profile(db)
    variant = AssetVariant(
        source_asset_id=asset.id,
        profile_id=profile.id,
        filename=f"v-{name}",
        status=status,
    )
    db.add(variant)
    await db.flush()
    return asset, variant


async def _job(db, variant, status, retry_count=0, error="cancelled by test"):
    job = Job(
        type=JobType.VARIANT_TRANSCODE,
        target_id=variant.id,
        status=status,
        retry_count=retry_count,
        error_message=error,
    )
    db.add(job)
    await db.flush()
    return job


class TestRepairs:
    async def test_cancelled_job_terminalises_a_stranded_variant(self, db_session):
        _asset, variant = await _variant(db_session)
        await _job(db_session, variant, JobStatus.CANCELLED)
        await db_session.commit()

        assert await reconcile_stranded_variants_once(db_session) == 1

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.CANCELLED

    async def test_poison_failed_job_terminalises_the_variant(self, db_session):
        """claim_job flips a job FAILED once retry_count exceeds the limit,
        but the mirroring helper no-opped on transcode jobs, so the variant
        sat at PENDING advertising work that will never run."""
        _asset, variant = await _variant(db_session)
        await _job(
            db_session, variant, JobStatus.FAILED,
            retry_count=MAX_JOB_RETRIES + 1, error="exceeded retry limit",
        )
        await db_session.commit()

        assert await reconcile_stranded_variants_once(db_session) == 1

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.FAILED

    async def test_a_processing_variant_is_repaired_too(self, db_session):
        _asset, variant = await _variant(db_session, status=VariantStatus.PROCESSING)
        await _job(db_session, variant, JobStatus.CANCELLED)
        await db_session.commit()

        assert await reconcile_stranded_variants_once(db_session) == 1

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.CANCELLED

    async def test_the_sweep_is_idempotent(self, db_session):
        _asset, variant = await _variant(db_session)
        await _job(db_session, variant, JobStatus.CANCELLED)
        await db_session.commit()

        assert await reconcile_stranded_variants_once(db_session) == 1
        assert await reconcile_stranded_variants_once(db_session) == 0


class TestRefusals:
    """Each of these is a way the cheap version of the sweep goes wrong."""

    async def test_an_older_active_job_protects_a_newer_terminal_one(
        self, db_session
    ):
        """The ordering here is the whole point, and it is not arbitrary.

        The stale-PROCESSING monitor re-enqueues by creating a *second* job
        without terminating the first, so the original can sit PROCESSING
        indefinitely. If that later job is then cancelled, the newest job is
        terminal while a genuinely active one is still outstanding. Only the
        candidate filter catches this: dispatching on the newest job alone
        would mark the variant CANCELLED out from under a running worker.

        With the opposite ordering the `else` fallthrough happens to return
        the same answer, which is why that arrangement cannot pin the guard.
        """
        _asset, variant = await _variant(db_session)
        await _job(db_session, variant, JobStatus.PROCESSING, error="")
        await _job(db_session, variant, JobStatus.CANCELLED)
        await db_session.commit()

        assert await reconcile_stranded_variants_once(db_session) == 0

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PENDING

    async def test_a_newer_queued_job_protects_an_older_terminal_one(
        self, db_session
    ):
        """The commoner arrangement: a job was cancelled and the work
        re-enqueued. Held by the `else` fallthrough rather than the candidate
        filter, but it is the case a profile edit actually produces."""
        _asset, variant = await _variant(db_session)
        await _job(db_session, variant, JobStatus.FAILED,
                   retry_count=MAX_JOB_RETRIES + 1)
        await _job(db_session, variant, JobStatus.PENDING, error="")
        await db_session.commit()

        assert await reconcile_stranded_variants_once(db_session) == 0

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PENDING

    async def test_failed_under_the_retry_limit_is_left_for_redelivery(
        self, db_session
    ):
        """claim_job reopens a FAILED job under the limit -- it increments
        retry_count and flips back to PROCESSING -- so FAILED alone is not a
        terminal outcome."""
        _asset, variant = await _variant(db_session)
        await _job(db_session, variant, JobStatus.FAILED, retry_count=1)
        await db_session.commit()

        assert await reconcile_stranded_variants_once(db_session) == 0

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PENDING

    async def test_a_done_job_is_never_synthesised_into_ready(self, db_session):
        """READY asserts a valid blob plus complete metadata. A job reporting
        success is not evidence of either, and a bogus READY variant would be
        served to devices."""
        _asset, variant = await _variant(db_session)
        await _job(db_session, variant, JobStatus.DONE, error="")
        await db_session.commit()

        assert await reconcile_stranded_variants_once(db_session) == 0

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PENDING

    async def test_a_variant_with_no_job_yet_is_left_alone(self, db_session):
        """The profiles router commits variants before enqueueing them, so
        this window is legitimate."""
        _asset, variant = await _variant(db_session)
        await db_session.commit()

        assert await reconcile_stranded_variants_once(db_session) == 0

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PENDING

    async def test_soft_deleted_assets_belong_to_the_other_reaper(self, db_session):
        _asset, variant = await _variant(db_session, asset_deleted=True)
        await _job(db_session, variant, JobStatus.CANCELLED)
        await db_session.commit()

        assert await reconcile_stranded_variants_once(db_session) == 0

    async def test_a_soft_deleted_variant_is_left_alone(self, db_session):
        from datetime import datetime, timezone
        _asset, variant = await _variant(db_session)
        variant.deleted_at = datetime.now(timezone.utc)
        await _job(db_session, variant, JobStatus.CANCELLED)
        await db_session.commit()

        assert await reconcile_stranded_variants_once(db_session) == 0

    async def test_an_already_terminal_variant_is_not_touched(self, db_session):
        _asset, variant = await _variant(db_session, status=VariantStatus.READY)
        await _job(db_session, variant, JobStatus.CANCELLED)
        await db_session.commit()

        assert await reconcile_stranded_variants_once(db_session) == 0

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.READY

    async def test_the_newest_job_decides_not_an_older_one(self, db_session):
        """An old CANCELLED job must not terminalise a variant whose newest
        job merely failed transiently and is awaiting redelivery."""
        _asset, variant = await _variant(db_session)
        await _job(db_session, variant, JobStatus.CANCELLED)
        await db_session.flush()
        # Newest job: FAILED but under the retry limit -> still retryable.
        newer = await _job(db_session, variant, JobStatus.FAILED, retry_count=1)
        from datetime import timedelta
        newer.created_at = newer.created_at + timedelta(seconds=30)
        await db_session.commit()

        assert await reconcile_stranded_variants_once(db_session) == 0

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PENDING


class TestBounding:
    async def test_the_sweep_is_bounded_by_limit(self, db_session):
        """The first run after deploy may face a large historical backlog."""
        for _ in range(5):
            _asset, variant = await _variant(db_session)
            await _job(db_session, variant, JobStatus.CANCELLED)
        await db_session.commit()

        assert await reconcile_stranded_variants_once(db_session, limit=2) == 2
        assert await reconcile_stranded_variants_once(db_session, limit=2) == 2
        assert await reconcile_stranded_variants_once(db_session, limit=2) == 1


class TestSupersessionUnblocked:
    async def test_reconciling_lets_the_supersession_sweep_see_the_row(
        self, db_session
    ):
        """The point of the whole exercise. supersede_ready_variants_once
        only considers READY/FAILED/CANCELLED variants, so a stranded PENDING
        row is invisible to it and lives forever."""
        from cms.services.transcoder import supersede_ready_variants_once
        from datetime import timedelta

        _asset, old = await _variant(db_session)
        await _job(db_session, old, JobStatus.CANCELLED)

        newer = AssetVariant(
            source_asset_id=old.source_asset_id,
            profile_id=old.profile_id,
            filename=f"newer-{uuid.uuid4().hex[:8]}.mp4",
            status=VariantStatus.READY,
        )
        db_session.add(newer)
        await db_session.flush()
        newer.created_at = old.created_at + timedelta(seconds=60)
        await db_session.commit()

        # Before reconciliation the stranded PENDING row is invisible.
        assert await supersede_ready_variants_once(db_session) == 0
        await db_session.refresh(old)
        assert old.deleted_at is None

        assert await reconcile_stranded_variants_once(db_session) == 1
        assert await supersede_ready_variants_once(db_session) == 1

        await db_session.refresh(old)
        assert old.deleted_at is not None


class TestPoisonHelper:
    """worker.transcoder.mark_variant_failed_on_exhaustion (the write-side fix).

    The imager helper returns "not_imager" for VARIANT_TRANSCODE, so before
    this helper existed a transcode job that exhausted MAX_JOB_RETRIES left
    its variant at PENDING permanently -- shown to the user as queued work
    that will never run.
    """

    @pytest_asyncio.fixture
    async def factory(self, db_engine):
        return async_sessionmaker(db_engine, expire_on_commit=False)

    async def _seed(self, factory, variant_status=VariantStatus.PENDING):
        async with factory() as db:
            _asset, variant = await _variant(db, status=variant_status)
            job = await _job(db, variant, JobStatus.FAILED,
                             retry_count=MAX_JOB_RETRIES + 1,
                             error="ffmpeg exploded")
            await db.commit()
            return variant.id, job

    async def test_marks_the_variant_failed(self, factory):
        variant_id, job = await self._seed(factory)

        assert await mark_variant_failed_on_exhaustion(factory, job) == "updated"

        async with factory() as db:
            v = (await db.execute(
                select(AssetVariant).where(AssetVariant.id == variant_id)
            )).scalar_one()
        assert v.status == VariantStatus.FAILED
        assert "exceeded retry limit" in (v.error_message or "")
        assert "ffmpeg exploded" in (v.error_message or "")

    async def test_ignores_non_transcode_jobs(self, factory):
        """Mirror of the imager helper's own type guard, in the other
        direction -- these two must not both claim the same job."""
        variant_id, job = await self._seed(factory)
        job.type = JobType.IMAGE_IMPORT

        assert await mark_variant_failed_on_exhaustion(factory, job) == "not_variant"

        async with factory() as db:
            v = (await db.execute(
                select(AssetVariant).where(AssetVariant.id == variant_id)
            )).scalar_one()
        assert v.status == VariantStatus.PENDING

    async def test_a_newer_job_owns_the_row(self, factory):
        """The stale-PROCESSING monitor re-enqueues by creating a new job
        rather than reusing the row, so a late poison kill for the old job
        must not stomp the retry that replaced it."""
        variant_id, job = await self._seed(factory)
        async with factory() as db:
            v = (await db.execute(
                select(AssetVariant).where(AssetVariant.id == variant_id)
            )).scalar_one()
            await _job(db, v, JobStatus.PENDING, error="")
            await db.commit()

        assert await mark_variant_failed_on_exhaustion(
            factory, job) == "skipped_newer_job"

        async with factory() as db:
            v = (await db.execute(
                select(AssetVariant).where(AssetVariant.id == variant_id)
            )).scalar_one()
        assert v.status == VariantStatus.PENDING

    async def test_never_downgrades_a_ready_variant(self, factory):
        variant_id, job = await self._seed(factory,
                                           variant_status=VariantStatus.READY)

        assert await mark_variant_failed_on_exhaustion(
            factory, job) == "skipped_status"

        async with factory() as db:
            v = (await db.execute(
                select(AssetVariant).where(AssetVariant.id == variant_id)
            )).scalar_one()
        assert v.status == VariantStatus.READY

    async def test_missing_variant_is_a_noop(self, factory):
        """Poison message arriving after the row was hard-deleted."""
        _variant_id, job = await self._seed(factory)
        job.target_id = uuid.uuid4()

        assert await mark_variant_failed_on_exhaustion(factory, job) == "missing"