"""Tests for the stale-presence sweep (PR #440).

Backstop sweep that flips ``devices.online=false`` for any device
whose ``last_seen`` is older than the threshold, then transitions
``device_alert_state.offline_since`` so the existing leader-gated
offline-alert pipeline can fire the notification.

Also covers the matching self-heal in ``device_presence.update_status``:
when a heartbeat lands for a device the sweep had marked offline, the
``online`` flag flips back to TRUE and any pending alert state is
cleared (or, if the offline notification had already fired, the
"back online" notification is emitted).
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_alert_state import DeviceAlertState
from cms.models.device_event import DeviceEvent, DeviceEventType
from cms.models.notification import Notification
from cms.services import device_presence
from cms.services.alert_service import (
    AlertService,
    STALE_PRESENCE_THRESHOLD_S,
    STALE_PRESENCE_BATCH_SIZE,
)


@pytest_asyncio.fixture
async def stale_seed(app):
    """Adopted+grouped device whose last heartbeat is past the threshold."""
    from cms.database import get_db
    factory = app.dependency_overrides[get_db]
    stale_ts = datetime.now(timezone.utc) - timedelta(
        seconds=STALE_PRESENCE_THRESHOLD_S + 30
    )
    async for db in factory():
        group = DeviceGroup(name="Stale Test Group")
        db.add(group)
        await db.flush()

        device = Device(
            id="stale-device-001",
            name="Stale Display",
            status=DeviceStatus.ADOPTED,
            group_id=group.id,
            online=True,
            last_seen=stale_ts,
        )
        db.add(device)
        await db.commit()
        yield {
            "device_id": device.id,
            "device_name": device.name,
            "group_id": group.id,
            "group_name": group.name,
        }
        break


@pytest_asyncio.fixture
def alert_svc():
    """Fresh AlertService with short grace for fast tests."""
    svc = AlertService()
    svc._offline_grace_seconds = 1
    return svc


class TestStalePresenceSweep:
    """``stale_presence_sweep_once`` claim + alert-state transition."""

    @pytest.mark.asyncio
    async def test_stale_device_flipped_offline(self, app, stale_seed, alert_svc):
        """Device past the threshold is flipped to online=false + alert state created."""
        info = stale_seed
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            flipped = await alert_svc.stale_presence_sweep_once(db)
            assert flipped == 1
            break

        async for db in factory():
            device = (await db.execute(
                select(Device).where(Device.id == info["device_id"])
            )).scalar_one()
            assert device.online is False
            assert device.connection_id is None

            state = (await db.execute(
                select(DeviceAlertState).where(
                    DeviceAlertState.device_id == info["device_id"]
                )
            )).scalar_one()
            assert state.offline_since is not None
            assert state.offline_notified is False

            events = (await db.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == info["device_id"],
                    DeviceEvent.event_type == DeviceEventType.OFFLINE,
                )
            )).scalars().all()
            assert len(events) == 1
            assert events[0].details == {"kind": "stale_heartbeat"}
            break

    @pytest.mark.asyncio
    async def test_recent_heartbeat_not_flipped(self, app, alert_svc):
        """Device with last_seen within the threshold is left alone."""
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        recent_ts = datetime.now(timezone.utc) - timedelta(
            seconds=STALE_PRESENCE_THRESHOLD_S - 5
        )
        async for db in factory():
            group = DeviceGroup(name="Fresh Group")
            db.add(group)
            await db.flush()
            device = Device(
                id="fresh-device-001",
                name="Fresh Device",
                status=DeviceStatus.ADOPTED,
                group_id=group.id,
                online=True,
                last_seen=recent_ts,
            )
            db.add(device)
            await db.commit()
            break

        async for db in factory():
            flipped = await alert_svc.stale_presence_sweep_once(db)
            assert flipped == 0
            break

        async for db in factory():
            device = (await db.execute(
                select(Device).where(Device.id == "fresh-device-001")
            )).scalar_one()
            assert device.online is True
            break

    @pytest.mark.asyncio
    async def test_already_offline_not_re_swept(self, app, alert_svc):
        """A device already online=false isn't re-claimed (idempotent)."""
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        stale_ts = datetime.now(timezone.utc) - timedelta(
            seconds=STALE_PRESENCE_THRESHOLD_S + 30
        )
        async for db in factory():
            group = DeviceGroup(name="Already Offline Group")
            db.add(group)
            await db.flush()
            device = Device(
                id="already-offline-001",
                status=DeviceStatus.ADOPTED,
                group_id=group.id,
                online=False,
                last_seen=stale_ts,
            )
            db.add(device)
            await db.commit()
            break

        async for db in factory():
            flipped = await alert_svc.stale_presence_sweep_once(db)
            assert flipped == 0
            break

    @pytest.mark.asyncio
    async def test_pending_device_flipped_no_alert(self, app, alert_svc):
        """Pending devices flip offline but get no event/alert state."""
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        stale_ts = datetime.now(timezone.utc) - timedelta(
            seconds=STALE_PRESENCE_THRESHOLD_S + 30
        )
        async for db in factory():
            device = Device(
                id="pending-stale-001",
                status=DeviceStatus.PENDING,
                online=True,
                last_seen=stale_ts,
            )
            db.add(device)
            await db.commit()
            break

        async for db in factory():
            flipped = await alert_svc.stale_presence_sweep_once(db)
            assert flipped == 1
            break

        async for db in factory():
            device = (await db.execute(
                select(Device).where(Device.id == "pending-stale-001")
            )).scalar_one()
            assert device.online is False
            # No alert state, no event for unadopted devices.
            state = (await db.execute(
                select(DeviceAlertState).where(
                    DeviceAlertState.device_id == "pending-stale-001"
                )
            )).scalar_one_or_none()
            assert state is None
            events = (await db.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == "pending-stale-001"
                )
            )).scalars().all()
            assert events == []
            break

    @pytest.mark.asyncio
    async def test_ungrouped_adopted_device_flipped_no_alert(self, app, alert_svc):
        """Adopted-but-ungrouped devices flip offline but get no event/alert state."""
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        stale_ts = datetime.now(timezone.utc) - timedelta(
            seconds=STALE_PRESENCE_THRESHOLD_S + 30
        )
        async for db in factory():
            device = Device(
                id="ungrouped-stale-001",
                status=DeviceStatus.ADOPTED,
                group_id=None,
                online=True,
                last_seen=stale_ts,
            )
            db.add(device)
            await db.commit()
            break

        async for db in factory():
            flipped = await alert_svc.stale_presence_sweep_once(db)
            assert flipped == 1
            break

        async for db in factory():
            device = (await db.execute(
                select(Device).where(Device.id == "ungrouped-stale-001")
            )).scalar_one()
            assert device.online is False
            events = (await db.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == "ungrouped-stale-001"
                )
            )).scalars().all()
            assert events == []
            break

    @pytest.mark.asyncio
    async def test_existing_offline_since_not_overwritten(self, app, stale_seed, alert_svc):
        """A device already in an offline_since window keeps its original timestamp."""
        info = stale_seed
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        prior_offline_since = datetime.now(timezone.utc) - timedelta(seconds=300)
        async for db in factory():
            db.add(DeviceAlertState(
                device_id=info["device_id"],
                offline_since=prior_offline_since,
                offline_notified=True,
            ))
            await db.commit()
            break

        async for db in factory():
            await alert_svc.stale_presence_sweep_once(db)
            break

        async for db in factory():
            state = (await db.execute(
                select(DeviceAlertState).where(
                    DeviceAlertState.device_id == info["device_id"]
                )
            )).scalar_one()
            # Original timestamp preserved (transition-only update).
            assert state.offline_since is not None
            stored = state.offline_since
            if stored.tzinfo is None:
                stored = stored.replace(tzinfo=timezone.utc)
            assert abs(
                (stored - prior_offline_since).total_seconds()
            ) < 1
            # offline_notified preserved (we don't reset already-notified).
            assert state.offline_notified is True
            break

    @pytest.mark.asyncio
    async def test_grace_then_offline_sweep_fires_notification(
        self, app, stale_seed, alert_svc,
    ):
        """End-to-end: stale-sweep flips offline → grace elapses → offline_sweep fires alert."""
        info = stale_seed
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]

        async for db in factory():
            await alert_svc.stale_presence_sweep_once(db)
            break

        # Wait past the (fixture-shortened) grace period.
        await asyncio.sleep(1.5)

        async for db in factory():
            emitted = await alert_svc.offline_sweep_once(db)
            assert emitted == 1
            break

        async for db in factory():
            notifs = (await db.execute(
                select(Notification).where(
                    Notification.group_id == info["group_id"],
                    Notification.level == "error",
                )
            )).scalars().all()
            assert len(notifs) == 1
            assert "offline" in notifs[0].title.lower()
            break

    @pytest.mark.asyncio
    async def test_batch_cap_caps_per_tick(self, app, alert_svc):
        """More stale devices than batch size are split across ticks."""
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        stale_ts = datetime.now(timezone.utc) - timedelta(
            seconds=STALE_PRESENCE_THRESHOLD_S + 30
        )
        target = STALE_PRESENCE_BATCH_SIZE + 5
        async for db in factory():
            group = DeviceGroup(name="Batch Group")
            db.add(group)
            await db.flush()
            for i in range(target):
                db.add(Device(
                    id=f"batch-stale-{i:03d}",
                    status=DeviceStatus.ADOPTED,
                    group_id=group.id,
                    online=True,
                    last_seen=stale_ts,
                ))
            await db.commit()
            break

        async for db in factory():
            flipped1 = await alert_svc.stale_presence_sweep_once(db)
            assert flipped1 == STALE_PRESENCE_BATCH_SIZE
            break

        async for db in factory():
            flipped2 = await alert_svc.stale_presence_sweep_once(db)
            assert flipped2 == target - STALE_PRESENCE_BATCH_SIZE
            break

        async for db in factory():
            still_online = (await db.execute(
                select(Device).where(
                    Device.id.like("batch-stale-%"),
                    Device.online.is_(True),
                )
            )).scalars().all()
            assert still_online == []
            break

    @pytest.mark.asyncio
    async def test_null_last_seen_not_flipped(self, app, alert_svc):
        """Device that has never reported (last_seen IS NULL) isn't claimed."""
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            group = DeviceGroup(name="Never Seen Group")
            db.add(group)
            await db.flush()
            device = Device(
                id="never-seen-001",
                status=DeviceStatus.ADOPTED,
                group_id=group.id,
                online=True,
                last_seen=None,
            )
            db.add(device)
            await db.commit()
            break

        async for db in factory():
            flipped = await alert_svc.stale_presence_sweep_once(db)
            assert flipped == 0
            break


class TestUpdateStatusSelfHeal:
    """``update_status`` re-flips online + clears alert state on heartbeat resume."""

    @pytest.mark.asyncio
    async def test_heartbeat_after_stale_offline_restores_online(
        self, app, stale_seed, alert_svc,
    ):
        """A device flipped offline by the sweep comes back online on the next heartbeat."""
        info = stale_seed
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]

        # Stale-sweep marks it offline.
        async for db in factory():
            await alert_svc.stale_presence_sweep_once(db)
            break

        async for db in factory():
            device = (await db.execute(
                select(Device).where(Device.id == info["device_id"])
            )).scalar_one()
            assert device.online is False
            break

        # Next heartbeat lands.
        async for db in factory():
            ok = await device_presence.update_status(
                db, info["device_id"],
                {"mode": "play", "asset": "x.mp4", "uptime_seconds": 1},
            )
            assert ok is True
            break

        # Allow the fire-and-forget reconnect dispatch to complete.
        await asyncio.sleep(0.2)

        async for db in factory():
            device = (await db.execute(
                select(Device).where(Device.id == info["device_id"])
            )).scalar_one()
            assert device.online is True

            state = (await db.execute(
                select(DeviceAlertState).where(
                    DeviceAlertState.device_id == info["device_id"]
                )
            )).scalar_one_or_none()
            # Either the alert state row was cleared, or the row never
            # had offline_notified set (so _record_reconnect's
            # else-branch ran). Either way, offline_since must be
            # None now so the next disconnect starts a fresh window.
            if state is not None:
                assert state.offline_since is None
            break

    @pytest.mark.asyncio
    async def test_heartbeat_after_grace_fires_back_online(
        self, app, stale_seed, alert_svc,
    ):
        """Device whose offline notification fired gets a back-online notification on heartbeat."""
        info = stale_seed
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]

        # Stale-sweep marks offline.
        async for db in factory():
            await alert_svc.stale_presence_sweep_once(db)
            break

        # Past grace: offline_sweep flips offline_notified=true and
        # emits the alert.
        await asyncio.sleep(1.5)
        async for db in factory():
            await alert_svc.offline_sweep_once(db)
            break

        # Heartbeat resumes.
        async for db in factory():
            await device_presence.update_status(
                db, info["device_id"],
                {"mode": "play", "asset": "y.mp4", "uptime_seconds": 1},
            )
            break

        # Wait for the dispatched _record_reconnect to land.
        await asyncio.sleep(0.5)

        async for db in factory():
            online_notifs = (await db.execute(
                select(Notification).where(
                    Notification.group_id == info["group_id"],
                    Notification.level == "success",
                )
            )).scalars().all()
            assert any(
                "back online" in n.title.lower() for n in online_notifs
            ), "Expected a back-online notification after heartbeat resume"

            state = (await db.execute(
                select(DeviceAlertState).where(
                    DeviceAlertState.device_id == info["device_id"]
                )
            )).scalar_one()
            assert state.offline_since is None
            assert state.offline_notified is False
            break

    @pytest.mark.asyncio
    async def test_steady_state_heartbeat_no_extra_event(self, app, stale_seed):
        """A normal heartbeat on an already-online device doesn't fire a reconnect."""
        info = stale_seed
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]

        # Pretend the device never went offline — bump it online.
        async for db in factory():
            await db.execute(
                update(Device)
                .where(Device.id == info["device_id"])
                .values(online=True, last_seen=datetime.now(timezone.utc))
            )
            await db.commit()
            break

        # Heartbeat.
        async for db in factory():
            await device_presence.update_status(
                db, info["device_id"],
                {"mode": "play", "asset": "z.mp4"},
            )
            break

        await asyncio.sleep(0.2)

        async for db in factory():
            online_events = (await db.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == info["device_id"],
                    DeviceEvent.event_type == DeviceEventType.ONLINE,
                )
            )).scalars().all()
            # No reconnect dispatched → no ONLINE event written.
            assert online_events == []
            break
