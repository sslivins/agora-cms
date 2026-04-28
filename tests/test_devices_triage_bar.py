"""Tests for the /devices triage bar helper (cms.services.device_alerts).

Pure-function tests — no DB, no fixtures. Verifies the severity tag
taxonomy and fleet count math that drive the Phase A triage bar.
"""

from types import SimpleNamespace

import pytest

from cms.services.device_alerts import (
    NEEDS_ATTENTION_TAGS,
    SEVERITY_TAGS,
    device_severity_tags,
    fleet_counts,
    is_needs_attention,
)


def _device(
    *,
    status: str = "adopted",
    is_online: bool = True,
    error: str | None = None,
    pipeline_state: str | None = None,
    cpu_temp_c: float | None = None,
    display_ports: list | None = None,
    display_connected: bool | None = None,
    storage_capacity_mb: int | None = None,
    storage_used_mb: int | None = None,
    update_available: bool = False,
    is_upgrading: bool = False,
):
    """Build a SimpleNamespace that quacks like a decorated Device row."""
    return SimpleNamespace(
        status=SimpleNamespace(value=status),
        is_online=is_online,
        error=error,
        pipeline_state=pipeline_state,
        cpu_temp_c=cpu_temp_c,
        display_ports=display_ports,
        display_connected=display_connected,
        storage_capacity_mb=storage_capacity_mb,
        storage_used_mb=storage_used_mb,
        update_available=update_available,
        is_upgrading=is_upgrading,
    )


# ── device_severity_tags ────────────────────────────────────────────


def test_healthy_device_has_no_tags():
    d = _device()
    assert device_severity_tags(d) == []


def test_pending_device_skipped():
    d = _device(status="pending", is_online=True)
    assert device_severity_tags(d) == []


def test_orphaned_device_only_orphaned_tag():
    # Orphaned subsumes other tags — even if the device claims to be
    # online with an error, we want the operator to see "orphaned"
    # not a misleading mix.
    d = _device(status="orphaned", is_online=True, error="boom")
    assert device_severity_tags(d) == ["orphaned"]


def test_offline_device_has_offline_tag_only():
    # Stale telemetry must not surface — even though display_connected
    # is False on this offline device, we suppress the display-off tag.
    d = _device(is_online=False, display_connected=False, error="stale")
    assert device_severity_tags(d) == ["offline"]


def test_online_error_tag_via_pipeline_state():
    d = _device(pipeline_state="ERROR")
    assert "error" in device_severity_tags(d)


def test_online_error_tag_via_error_field():
    d = _device(error="player crashed")
    assert "error" in device_severity_tags(d)


def test_display_off_via_ports_all_disconnected():
    d = _device(display_ports=[
        {"port": "HDMI-1", "connected": False},
        {"port": "HDMI-2", "connected": False},
    ])
    assert "display-off" in device_severity_tags(d)


def test_display_off_legacy_path():
    d = _device(display_ports=None, display_connected=False)
    assert "display-off" in device_severity_tags(d)


def test_display_partially_connected_no_tag():
    d = _device(display_ports=[
        {"port": "HDMI-1", "connected": True},
        {"port": "HDMI-2", "connected": False},
    ])
    assert "display-off" not in device_severity_tags(d)


def test_storage_critical_under_5pct():
    d = _device(storage_capacity_mb=10000, storage_used_mb=9700)  # 3% free
    assert "storage-critical" in device_severity_tags(d)


def test_storage_low_not_critical():
    d = _device(storage_capacity_mb=10000, storage_used_mb=9200)  # 8% free
    # Storage Low (<10%) is *not* in the triage taxonomy — only
    # Critical is. Storage Low is still rendered as a per-row chip but
    # doesn't fire the triage filter.
    assert "storage-critical" not in device_severity_tags(d)


def test_maintenance_tag_for_update_available():
    d = _device(update_available=True)
    assert "maintenance" in device_severity_tags(d)


def test_maintenance_tag_for_upgrading():
    d = _device(is_upgrading=True)
    assert "maintenance" in device_severity_tags(d)


def test_multiple_tags_can_apply():
    d = _device(
        error="boom",
        display_ports=[{"port": "HDMI-1", "connected": False}],
        storage_capacity_mb=100,
        storage_used_mb=99,
        update_available=True,
    )
    tags = set(device_severity_tags(d))
    assert {"error", "display-off", "storage-critical", "maintenance"} <= tags


# ── is_needs_attention ──────────────────────────────────────────────


def test_needs_attention_excludes_maintenance_and_healthy():
    assert is_needs_attention(["maintenance"]) is False
    assert is_needs_attention([]) is False
    assert is_needs_attention(["error"]) is True
    assert is_needs_attention(["offline", "maintenance"]) is True


# ── fleet_counts ────────────────────────────────────────────────────


def test_fleet_counts_excludes_pending():
    fleet = [
        _device(),  # healthy adopted
        _device(status="pending", is_online=True),
    ]
    counts = fleet_counts(fleet)
    assert counts["all"] == 1
    assert counts["healthy"] == 1


def test_fleet_counts_aggregate():
    fleet = [
        _device(),                                     # healthy
        _device(),                                     # healthy
        _device(is_online=False),                      # offline
        _device(error="x"),                            # error
        _device(error="y", update_available=True),     # error + maintenance
        _device(status="orphaned", is_online=False),   # orphaned
        _device(storage_capacity_mb=100, storage_used_mb=99),  # storage-critical
    ]
    counts = fleet_counts(fleet)
    assert counts["all"] == 7
    assert counts["healthy"] == 2
    assert counts["error"] == 2
    assert counts["offline"] == 1
    assert counts["orphaned"] == 1
    assert counts["storage-critical"] == 1
    assert counts["maintenance"] == 1
    # needs_attention = error + offline + display-off + storage-crit + orphaned,
    # de-duplicated per device. 2 error + 1 offline + 1 orphaned + 1 storage = 5.
    assert counts["needs_attention"] == 5


def test_fleet_counts_keys_present():
    counts = fleet_counts([])
    assert counts["all"] == 0
    assert counts["healthy"] == 0
    assert counts["needs_attention"] == 0
    for tag in SEVERITY_TAGS:
        assert tag in counts


def test_needs_attention_set_is_a_subset_of_severity_tags():
    # Sanity guard against typos in the constants.
    assert NEEDS_ATTENTION_TAGS <= set(SEVERITY_TAGS)
