"""Unit tests for the central device-config validation service (Stage 1).

Pure-core coverage (no DB): per-device conflict detection reframed as the union
across a device's group set, introduced-vs-preexisting diffing, and per-device
capability gating.
"""

import uuid
from datetime import time

import pytest

from cms.models.asset import Asset, AssetType
from cms.models.schedule import Schedule
from cms.schemas.protocol import (
    CAPABILITY_SLIDESHOW_COMPOSED_V1,
    CAPABILITY_SLIDESHOW_V1,
)
from cms.services.device_config_validation import (
    CapabilityFailure,
    capability_failures_for_device,
    diff_conflicts,
    find_effective_conflicts,
    validate_device_effective_config,
)


def _sched(
    start_time,
    end_time,
    *,
    name="s",
    group_id=None,
    priority=0,
    days_of_week=None,
    enabled=True,
    asset=None,
    sched_id=None,
) -> Schedule:
    s = Schedule(
        name=name,
        asset_id=uuid.uuid4(),
        group_id=group_id or uuid.uuid4(),
        enabled=enabled,
        start_time=start_time,
        end_time=end_time,
        days_of_week=days_of_week,
        priority=priority,
    )
    s.id = sched_id or uuid.uuid4()
    if asset is not None:
        s.asset = asset
    return s


def _asset(asset_type, *, filename="a", asset_id=None) -> Asset:
    a = Asset(filename=filename, asset_type=asset_type, size_bytes=1, checksum="c")
    a.id = asset_id or uuid.uuid4()
    return a


class TestFindEffectiveConflicts:
    def test_same_priority_overlap_conflicts(self):
        a = _sched(time(9, 0), time(11, 0), priority=5, name="A")
        b = _sched(time(10, 0), time(12, 0), priority=5, name="B")
        conflicts = find_effective_conflicts([a, b])
        assert len(conflicts) == 1
        pair = next(iter(conflicts.values()))
        assert {pair.schedule_a_name, pair.schedule_b_name} == {"A", "B"}
        assert pair.priority == 5

    def test_different_priority_no_conflict(self):
        a = _sched(time(9, 0), time(11, 0), priority=5)
        b = _sched(time(10, 0), time(12, 0), priority=6)
        assert find_effective_conflicts([a, b]) == {}

    def test_no_time_overlap_no_conflict(self):
        a = _sched(time(9, 0), time(10, 0), priority=5)
        b = _sched(time(10, 0), time(11, 0), priority=5)
        assert find_effective_conflicts([a, b]) == {}

    def test_disabled_schedule_never_conflicts(self):
        a = _sched(time(9, 0), time(11, 0), priority=5)
        b = _sched(time(10, 0), time(12, 0), priority=5, enabled=False)
        assert find_effective_conflicts([a, b]) == {}

    def test_conflict_across_different_groups(self):
        # The whole point of the reframing: two schedules in DIFFERENT groups
        # conflict once both are effective for the same device.
        a = _sched(time(9, 0), time(11, 0), priority=5, group_id=uuid.uuid4())
        b = _sched(time(10, 0), time(12, 0), priority=5, group_id=uuid.uuid4())
        assert len(find_effective_conflicts([a, b])) == 1

    def test_three_way_pairs(self):
        a = _sched(time(9, 0), time(12, 0), priority=5, name="A")
        b = _sched(time(10, 0), time(11, 0), priority=5, name="B")
        c = _sched(time(11, 30), time(13, 0), priority=5, name="C")
        conflicts = find_effective_conflicts([a, b, c])
        # A-B overlap, A-C overlap, B-C do not.
        assert len(conflicts) == 2

    def test_pair_key_is_order_independent(self):
        a = _sched(time(9, 0), time(11, 0), priority=5)
        b = _sched(time(10, 0), time(12, 0), priority=5)
        k1 = set(find_effective_conflicts([a, b]).keys())
        k2 = set(find_effective_conflicts([b, a]).keys())
        assert k1 == k2


class TestDiffConflicts:
    def test_introduced_vs_preexisting(self):
        a = _sched(time(9, 0), time(11, 0), priority=5, name="A")
        b = _sched(time(10, 0), time(12, 0), priority=5, name="B")
        c = _sched(time(10, 30), time(12, 30), priority=5, name="C")
        before = find_effective_conflicts([a, b])
        after = find_effective_conflicts([a, b, c])
        introduced, preexisting = diff_conflicts(before, after)
        # A-B was already there; adding C introduces A-C and B-C.
        assert len(preexisting) == 1
        assert len(introduced) == 2

    def test_no_change(self):
        a = _sched(time(9, 0), time(11, 0), priority=5)
        b = _sched(time(10, 0), time(12, 0), priority=5)
        conflicts = find_effective_conflicts([a, b])
        introduced, preexisting = diff_conflicts(conflicts, conflicts)
        assert introduced == []
        assert len(preexisting) == 1


class TestCapabilityFailures:
    def test_webpage_requires_pi5(self):
        s = _sched(time(9, 0), time(10, 0), asset=_asset(AssetType.WEBPAGE))
        fails = capability_failures_for_device("Raspberry Pi 4 Model B", [], [s])
        assert len(fails) == 1
        assert fails[0].required_capability == "raspberry_pi_5"

    def test_webpage_ok_on_pi5(self):
        s = _sched(time(9, 0), time(10, 0), asset=_asset(AssetType.WEBPAGE))
        assert capability_failures_for_device("Raspberry Pi 5 Model B", [], [s]) == []

    def test_stream_requires_pi5(self):
        s = _sched(time(9, 0), time(10, 0), asset=_asset(AssetType.STREAM))
        fails = capability_failures_for_device("Raspberry Pi 4", [], [s])
        assert len(fails) == 1

    def test_slideshow_requires_capability(self):
        s = _sched(time(9, 0), time(10, 0), asset=_asset(AssetType.SLIDESHOW))
        fails = capability_failures_for_device("Raspberry Pi 4", [], [s])
        assert len(fails) == 1
        assert fails[0].required_capability == CAPABILITY_SLIDESHOW_V1

    def test_slideshow_ok_with_capability(self):
        s = _sched(time(9, 0), time(10, 0), asset=_asset(AssetType.SLIDESHOW))
        assert (
            capability_failures_for_device(
                "Raspberry Pi 4", [CAPABILITY_SLIDESHOW_V1], [s]
            )
            == []
        )

    def test_composed_slideshow_requires_composed_capability(self):
        aid = uuid.uuid4()
        s = _sched(
            time(9, 0), time(10, 0),
            asset=_asset(AssetType.SLIDESHOW, asset_id=aid),
        )
        fails = capability_failures_for_device(
            "Raspberry Pi 4",
            [CAPABILITY_SLIDESHOW_V1],
            [s],
            composed_slideshow_asset_ids={str(aid)},
        )
        assert len(fails) == 1
        assert fails[0].required_capability == CAPABILITY_SLIDESHOW_COMPOSED_V1

    def test_composed_slideshow_ok_with_both_capabilities(self):
        aid = uuid.uuid4()
        s = _sched(
            time(9, 0), time(10, 0),
            asset=_asset(AssetType.SLIDESHOW, asset_id=aid),
        )
        assert (
            capability_failures_for_device(
                "Raspberry Pi 4",
                [CAPABILITY_SLIDESHOW_V1, CAPABILITY_SLIDESHOW_COMPOSED_V1],
                [s],
                composed_slideshow_asset_ids={str(aid)},
            )
            == []
        )

    def test_video_needs_no_capability(self):
        s = _sched(time(9, 0), time(10, 0), asset=_asset(AssetType.VIDEO))
        assert capability_failures_for_device("Raspberry Pi 4", [], [s]) == []

    def test_disabled_schedule_skipped(self):
        s = _sched(
            time(9, 0), time(10, 0),
            enabled=False,
            asset=_asset(AssetType.WEBPAGE),
        )
        assert capability_failures_for_device("Raspberry Pi 4", [], [s]) == []


class TestValidateDeviceEffectiveConfig:
    def test_introduced_conflict_blocks(self):
        a = _sched(time(9, 0), time(11, 0), priority=5)
        b = _sched(time(10, 0), time(12, 0), priority=5)
        res = validate_device_effective_config(
            "dev-1", "Raspberry Pi 4", [],
            before_schedules=[a],
            after_schedules=[a, b],
        )
        assert res.is_blocked
        assert len(res.introduced_conflicts) == 1
        assert res.preexisting_conflicts == []

    def test_preexisting_conflict_warns_not_blocks(self):
        a = _sched(time(9, 0), time(11, 0), priority=5)
        b = _sched(time(10, 0), time(12, 0), priority=5)
        # Both present before and after (e.g. removing an unrelated 3rd group).
        res = validate_device_effective_config(
            "dev-1", "Raspberry Pi 4", [],
            before_schedules=[a, b],
            after_schedules=[a, b],
        )
        assert not res.is_blocked
        assert res.has_warnings
        assert len(res.preexisting_conflicts) == 1

    def test_from_nothing_adoption_blocks_on_conflict(self):
        a = _sched(time(9, 0), time(11, 0), priority=5)
        b = _sched(time(10, 0), time(12, 0), priority=5)
        res = validate_device_effective_config(
            "dev-1", "Raspberry Pi 4", [],
            before_schedules=None,
            after_schedules=[a, b],
        )
        # No before-state => every conflict is introduced.
        assert res.is_blocked
        assert len(res.introduced_conflicts) == 1

    def test_capability_failure_blocks(self):
        s = _sched(time(9, 0), time(10, 0), asset=_asset(AssetType.WEBPAGE))
        res = validate_device_effective_config(
            "dev-1", "Raspberry Pi 4", [],
            before_schedules=None,
            after_schedules=[s],
        )
        assert res.is_blocked
        assert isinstance(res.capability_failures[0], CapabilityFailure)


@pytest.mark.asyncio
class TestValidateDeviceGroupTransitionDB:
    async def _seed(self, db_session):
        from cms.models.asset import Asset as AssetModel, AssetType as AT
        from cms.models.device import Device, DeviceGroup, DeviceStatus
        from cms.models.schedule import Schedule as Sched

        g1 = DeviceGroup(name="G1")
        g2 = DeviceGroup(name="G2")
        dev = Device(
            id="val-pi",
            name="Val Pi",
            status=DeviceStatus.ADOPTED,
            device_type="Raspberry Pi 4",
        )
        asset = AssetModel(
            filename="v.mp4", asset_type=AT.VIDEO, size_bytes=1, checksum="z"
        )
        db_session.add_all([g1, g2, dev, asset])
        await db_session.flush()
        # Overlapping, equal-priority schedules in DIFFERENT groups.
        s1 = Sched(
            name="S1", asset_id=asset.id, group_id=g1.id, enabled=True,
            start_time=time(9, 0), end_time=time(11, 0), priority=5,
        )
        s2 = Sched(
            name="S2", asset_id=asset.id, group_id=g2.id, enabled=True,
            start_time=time(10, 0), end_time=time(12, 0), priority=5,
        )
        db_session.add_all([s1, s2])
        await db_session.commit()
        return str(dev.id), g1.id, g2.id

    async def test_single_group_no_conflict(self, db_session):
        from cms.services.device_config_validation import (
            validate_device_group_transition,
        )
        dev_id, g1, g2 = await self._seed(db_session)
        res = await validate_device_group_transition(
            db_session, after_membership={dev_id: {g1}}
        )
        assert not res.is_blocked

    async def test_adding_second_group_introduces_conflict(self, db_session):
        from cms.services.device_config_validation import (
            validate_device_group_transition,
        )
        dev_id, g1, g2 = await self._seed(db_session)
        res = await validate_device_group_transition(
            db_session,
            before_membership={dev_id: {g1}},
            after_membership={dev_id: {g1, g2}},
        )
        assert res.is_blocked
        assert len(res.all_introduced_conflicts()) == 1

    async def test_unadopted_device_skipped(self, db_session):
        from cms.models.device import Device, DeviceStatus
        from cms.services.device_config_validation import (
            validate_device_group_transition,
        )
        dev_id, g1, g2 = await self._seed(db_session)
        dev = await db_session.get(Device, dev_id)
        dev.status = DeviceStatus.PENDING
        await db_session.commit()
        res = await validate_device_group_transition(
            db_session, after_membership={dev_id: {g1, g2}}
        )
        # Pending device receives no sync => not validated => not blocked.
        assert not res.is_blocked
        assert dev_id not in res.devices
