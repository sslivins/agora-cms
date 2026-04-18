"""Unit tests for cms.services.audit_service.compute_diff."""

import uuid
from datetime import datetime, time
from types import SimpleNamespace

from cms.services.audit_service import compute_diff


def test_returns_only_changed_fields():
    obj = SimpleNamespace(name="old", count=5)
    diff = compute_diff(obj, {"name": "new", "count": 5})
    assert diff == {"name": {"old": "old", "new": "new"}}


def test_no_changes_returns_empty():
    obj = SimpleNamespace(a=1, b=2)
    assert compute_diff(obj, {"a": 1, "b": 2}) == {}


def test_serializes_uuid():
    old_id = uuid.uuid4()
    new_id = uuid.uuid4()
    obj = SimpleNamespace(group_id=old_id)
    diff = compute_diff(obj, {"group_id": new_id})
    assert diff["group_id"] == {"old": str(old_id), "new": str(new_id)}


def test_serializes_datetime_and_time():
    obj = SimpleNamespace(start_time=time(8, 0), updated_at=datetime(2024, 1, 1, 12, 0))
    diff = compute_diff(
        obj,
        {"start_time": time(9, 0), "updated_at": datetime(2024, 1, 2, 12, 0)},
    )
    assert diff["start_time"]["old"] == "08:00:00"
    assert diff["start_time"]["new"] == "09:00:00"
    assert diff["updated_at"]["old"].startswith("2024-01-01T")


def test_skips_unknown_attribute():
    obj = SimpleNamespace(name="x")
    diff = compute_diff(obj, {"name": "y", "ghost": "z"})
    assert "ghost" not in diff
    assert "name" in diff


def test_exclude_set_is_honoured():
    obj = SimpleNamespace(a=1, b=2)
    diff = compute_diff(obj, {"a": 99, "b": 88}, exclude={"a"})
    assert diff == {"b": {"old": 2, "new": 88}}
