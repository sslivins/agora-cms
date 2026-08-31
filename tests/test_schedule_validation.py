"""Unit tests for ScheduleCreate/ScheduleUpdate schema validators.

These are pure schema-level checks (no DB) covering the input-integrity
guards added alongside the occurrence overlap engine.
"""

import uuid

import pytest
from pydantic import ValidationError

from cms.schemas.schedule import ScheduleCreate, ScheduleUpdate


def _create_kwargs(**overrides):
    base = {
        "name": "s",
        "asset_id": uuid.uuid4(),
        "group_id": uuid.uuid4(),
        "start_time": "09:00",
        "end_time": "10:00",
    }
    base.update(overrides)
    return base


class TestDaysOfWeekValidation:
    def test_valid_days_pass(self):
        s = ScheduleCreate(**_create_kwargs(days_of_week=[1, 2, 3]))
        assert s.days_of_week == [1, 2, 3]

    def test_days_deduped_and_sorted(self):
        s = ScheduleCreate(**_create_kwargs(days_of_week=[5, 1, 5, 3]))
        assert s.days_of_week == [1, 3, 5]

    def test_none_days_allowed(self):
        s = ScheduleCreate(**_create_kwargs(days_of_week=None))
        assert s.days_of_week is None

    @pytest.mark.parametrize("bad", [[0], [8], [1, 8], [-1]])
    def test_out_of_range_rejected(self, bad):
        with pytest.raises(ValidationError):
            ScheduleCreate(**_create_kwargs(days_of_week=bad))

    def test_update_validates_days(self):
        with pytest.raises(ValidationError):
            ScheduleUpdate(days_of_week=[0])
        assert ScheduleUpdate(days_of_week=[2, 1, 2]).days_of_week == [1, 2]


class TestUpdateNotNull:
    def test_omitted_priority_ok(self):
        # Field omitted -> validator not run, stays None (not sent to DB).
        assert ScheduleUpdate(name="x").priority is None

    def test_explicit_null_priority_rejected(self):
        with pytest.raises(ValidationError):
            ScheduleUpdate(priority=None)

    def test_valid_priority(self):
        assert ScheduleUpdate(priority=7).priority == 7

    def test_omitted_enabled_ok(self):
        assert ScheduleUpdate(name="x").enabled is None

    def test_explicit_null_enabled_rejected(self):
        with pytest.raises(ValidationError):
            ScheduleUpdate(enabled=None)
