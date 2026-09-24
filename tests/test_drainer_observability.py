"""``/healthz/system`` must report outbox drainer liveness.

Jobs reach workers only through the outbox. If the drainer stalls, the
system presents as "nothing is transcoding" — no exception, no failed job,
no alert. The age of the oldest undrained row is the only signal that
distinguishes a stalled drainer from an idle system, and until now it was
computed by a helper nothing called.

The tests below pin two things that are easy to get wrong in opposite
directions:

* a *small* backlog must not mark the deploy degraded — post-deploy verify
  calls this endpoint, and the drainer is allowed to be a tick behind;
* a *large* backlog must, or the signal is decorative.

They also pin that this lives on ``/healthz/system`` and not ``/healthz``:
``/healthz`` is the container liveness probe, so reporting a backlog there
would have the platform restart the replica — which does nothing for a
stalled drainer and drops in-flight requests.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from shared.models.job import Job, JobOutbox, JobStatus, JobType
from shared.services.jobs import (
    OUTBOX_AGE_ERROR_SECONDS,
    OUTBOX_AGE_WARN_SECONDS,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def healthy_config(monkeypatch):
    """Neutralise the unrelated deploy-config finding.

    The test app has no ``AGORA_CMS_BASE_URL``, so ``/healthz/system``
    reports ``degraded`` before the outbox is even consulted. Asserting on
    ``status`` without this fixture passes no matter what the outbox does —
    the first draft of these tests did exactly that and proved nothing.
    """
    import cms.deploy_config

    monkeypatch.setattr(
        cms.deploy_config, "detect_missing_deploy_config", lambda _settings: []
    )


async def _outbox_row(db, *, age_seconds: float):
    job = Job(
        type=JobType.VARIANT_TRANSCODE,
        target_id=uuid.uuid4(),
        status=JobStatus.PENDING,
    )
    db.add(job)
    await db.flush()
    row = JobOutbox(job_id=job.id)
    row.created_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    db.add(row)
    await db.commit()
    return row


class TestThresholds:
    async def test_warn_is_below_error(self):
        """A warn above error would make 'backlogged' unreachable."""
        assert OUTBOX_AGE_WARN_SECONDS < OUTBOX_AGE_ERROR_SECONDS

    async def test_error_threshold_is_generous_enough_for_a_deploy(self):
        """Post-deploy verify hits this endpoint; a tight threshold would
        make deploys flaky rather than make stalls visible."""
        assert OUTBOX_AGE_ERROR_SECONDS >= 120


class TestHealthzSystem:
    async def test_empty_outbox_is_ok_and_reports_no_age(
        self, unauthed_client, db_session
    ):
        resp = await unauthed_client.get("/healthz/system")

        assert resp.status_code == 200
        outbox = resp.json()["outbox"]
        assert outbox["ok"] is True
        assert outbox["oldest_age_seconds"] is None
        assert outbox["backlogged"] is False

    async def test_baseline_is_ok_so_the_status_assertions_below_mean_something(
        self, unauthed_client, healthy_config
    ):
        """Guards the guard: if this goes red, every ``status`` assertion in
        this class is vacuous."""
        body = (await unauthed_client.get("/healthz/system")).json()

        assert body["status"] == "ok"

    async def test_fresh_backlog_is_reported_but_not_degraded(
        self, unauthed_client, db_session, healthy_config
    ):
        """The drainer being a tick behind is normal."""
        await _outbox_row(db_session, age_seconds=2)

        body = (await unauthed_client.get("/healthz/system")).json()

        assert body["outbox"]["oldest_age_seconds"] is not None
        assert body["outbox"]["backlogged"] is False
        assert body["outbox"]["ok"] is True
        assert body["status"] == "ok"

    async def test_backlog_past_warn_is_flagged_but_not_degraded(
        self, unauthed_client, db_session, healthy_config
    ):
        await _outbox_row(db_session, age_seconds=OUTBOX_AGE_WARN_SECONDS + 30)

        body = (await unauthed_client.get("/healthz/system")).json()

        assert body["outbox"]["backlogged"] is True, "warn threshold not applied"
        assert body["outbox"]["ok"] is True, (
            "a warn-level backlog must not fail a deploy"
        )
        assert body["status"] == "ok"

    async def test_backlog_past_error_degrades_the_system(
        self, unauthed_client, db_session, healthy_config
    ):
        """The load-bearing one: a genuinely stalled drainer must be loud."""
        await _outbox_row(db_session, age_seconds=OUTBOX_AGE_ERROR_SECONDS + 60)

        body = (await unauthed_client.get("/healthz/system")).json()

        assert body["outbox"]["ok"] is False
        assert body["status"] == "degraded"

    async def test_oldest_row_decides_not_the_newest(
        self, unauthed_client, db_session, healthy_config
    ):
        """A steady trickle of new rows must not mask one stuck row."""
        await _outbox_row(db_session, age_seconds=OUTBOX_AGE_ERROR_SECONDS + 60)
        await _outbox_row(db_session, age_seconds=1)

        body = (await unauthed_client.get("/healthz/system")).json()

        assert body["outbox"]["ok"] is False
        assert body["outbox"]["oldest_age_seconds"] > OUTBOX_AGE_ERROR_SECONDS

    async def test_thresholds_are_published_in_the_response(
        self, unauthed_client
    ):
        """Monitoring callers should not have to hardcode our constants."""
        outbox = (await unauthed_client.get("/healthz/system")).json()["outbox"]

        assert outbox["warn_threshold_seconds"] == OUTBOX_AGE_WARN_SECONDS
        assert outbox["error_threshold_seconds"] == OUTBOX_AGE_ERROR_SECONDS


class TestLivenessProbeIsUnaffected:
    async def test_healthz_stays_ok_under_a_stalled_outbox(
        self, unauthed_client, db_session
    ):
        """If this ever fails, the platform will start restarting replicas
        in response to a drainer stall, which cannot possibly fix it."""
        await _outbox_row(db_session, age_seconds=OUTBOX_AGE_ERROR_SECONDS * 10)

        resp = await unauthed_client.get("/healthz")

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_healthz_does_not_report_outbox(self, unauthed_client):
        """Keeps the liveness probe cheap — no extra query per probe tick."""
        body = (await unauthed_client.get("/healthz")).json()

        assert "outbox" not in body
