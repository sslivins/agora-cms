"""Unit tests for :mod:`cms.services.device_presence`.

Covers the Stage 2c helper surface that replaces the in-memory
``DeviceManager`` state — presence flips, STATUS UPDATE + monotonic
guard, error_since latching, list_states shape, flag setters.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from cms.models.device import Device, DeviceStatus
from cms.services import device_presence


@pytest_asyncio.fixture
async def _device(db_session):
    """Insert one device row and return it."""
    d = Device(
        id="d-presence-1",
        name="Test Device",
        status=DeviceStatus.ADOPTED,
    )
    db_session.add(d)
    await db_session.commit()
    return d


class TestMarkOnlineOffline:
    @pytest.mark.asyncio
    async def test_mark_online_sets_flag_and_connection_id(self, db_session, _device):
        await device_presence.mark_online(db_session, _device.id, connection_id="wps-abc")
        row = (await db_session.execute(
            select(Device.online, Device.connection_id, Device.last_seen).where(Device.id == _device.id)
        )).one()
        assert row.online is True
        assert row.connection_id == "wps-abc"
        assert row.last_seen is not None

    @pytest.mark.asyncio
    async def test_mark_online_without_connection_id(self, db_session, _device):
        await device_presence.mark_online(db_session, _device.id)
        row = (await db_session.execute(
            select(Device.online, Device.connection_id).where(Device.id == _device.id)
        )).one()
        assert row.online is True
        assert row.connection_id is None

    @pytest.mark.asyncio
    async def test_mark_offline_clears_flag_and_connection_id(self, db_session, _device):
        await device_presence.mark_online(db_session, _device.id, connection_id="x")
        await device_presence.mark_offline(db_session, _device.id)
        row = (await db_session.execute(
            select(Device.online, Device.connection_id).where(Device.id == _device.id)
        )).one()
        assert row.online is False
        assert row.connection_id is None

    @pytest.mark.asyncio
    async def test_mark_online_unknown_device_is_noop(self, db_session):
        # No row for this id — must not raise (row just matches zero times).
        await device_presence.mark_online(db_session, "ghost")
        await device_presence.mark_offline(db_session, "ghost")


class TestUpdateStatus:
    @pytest.mark.asyncio
    async def test_applies_telemetry_fields(self, db_session, _device):
        ok = await device_presence.update_status(
            db_session, _device.id,
            {
                "mode": "play",
                "asset": "video.mp4",
                "pipeline_state": "PLAYING",
                "uptime_seconds": 42,
                "cpu_temp_c": 51.5,
                "playback_position_ms": 1234,
                "ssh_enabled": True,
                "local_api_enabled": True,
                "display_connected": True,
                "display_ports": [
                    {"name": "HDMI-1", "connected": True},
                    {"name": "HDMI-2", "connected": False},
                ],
            },
        )
        assert ok is True
        row = (await db_session.execute(
            select(Device).where(Device.id == _device.id)
        )).scalar_one()
        assert row.mode == "play"
        assert row.asset == "video.mp4"
        assert row.pipeline_state == "PLAYING"
        assert row.uptime_seconds == 42
        assert row.cpu_temp_c == 51.5
        assert row.playback_position_ms == 1234
        assert row.ssh_enabled is True
        assert row.display_connected is True
        assert row.display_ports == [
            {"name": "HDMI-1", "connected": True},
            {"name": "HDMI-2", "connected": False},
        ]
        assert row.last_status_ts is not None

    @pytest.mark.asyncio
    async def test_display_ports_absent_does_not_clobber(self, db_session, _device):
        # First STATUS reports two ports.
        await device_presence.update_status(
            db_session, _device.id,
            {"display_ports": [{"name": "HDMI-1", "connected": True}]},
        )
        # Second STATUS omits the field — must NOT wipe the previous list.
        await device_presence.update_status(
            db_session, _device.id, {"mode": "play"},
        )
        row = (await db_session.execute(
            select(Device.display_ports).where(Device.id == _device.id)
        )).scalar_one()
        assert row == [{"name": "HDMI-1", "connected": True}]

    @pytest.mark.asyncio
    async def test_monotonic_guard_rejects_older_status(self, db_session, _device):
        t1 = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        t0 = t1 - timedelta(seconds=30)

        ok1 = await device_presence.update_status(
            db_session, _device.id, {"mode": "play"}, status_ts=t1,
        )
        assert ok1 is True

        # Older STATUS — guard must skip and return False
        ok2 = await device_presence.update_status(
            db_session, _device.id, {"mode": "splash"}, status_ts=t0,
        )
        assert ok2 is False

        row = (await db_session.execute(
            select(Device.mode, Device.last_status_ts).where(Device.id == _device.id)
        )).one()
        assert row.mode == "play"  # unchanged
        # Compare naive-aware by stripping tz for SQLite compatibility
        stored_ts = row.last_status_ts
        if stored_ts.tzinfo is None:
            stored_ts = stored_ts.replace(tzinfo=timezone.utc)
        assert stored_ts == t1

    @pytest.mark.asyncio
    async def test_equal_timestamp_is_noop(self, db_session, _device):
        """Strict less-than guard: same ts applied twice is a no-op second time."""
        t = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        ok1 = await device_presence.update_status(
            db_session, _device.id, {"mode": "play"}, status_ts=t,
        )
        ok2 = await device_presence.update_status(
            db_session, _device.id, {"mode": "stop"}, status_ts=t,
        )
        assert ok1 is True
        assert ok2 is False

    @pytest.mark.asyncio
    async def test_error_since_latches_on_first_error(self, db_session, _device):
        t0 = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(seconds=30)

        await device_presence.update_status(
            db_session, _device.id, {"error": "first-error"}, status_ts=t0,
        )
        row0 = (await db_session.execute(
            select(Device.error, Device.error_since).where(Device.id == _device.id)
        )).one()
        assert row0.error == "first-error"
        assert row0.error_since is not None
        since0 = row0.error_since

        # Same error again — error_since must NOT reset
        await device_presence.update_status(
            db_session, _device.id, {"error": "first-error"}, status_ts=t1,
        )
        row1 = (await db_session.execute(
            select(Device.error_since).where(Device.id == _device.id)
        )).one()
        assert row1.error_since == since0

    @pytest.mark.asyncio
    async def test_error_since_clears_when_error_resolves(self, db_session, _device):
        t0 = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(seconds=30)

        await device_presence.update_status(
            db_session, _device.id, {"error": "boom"}, status_ts=t0,
        )
        await device_presence.update_status(
            db_session, _device.id, {"error": None}, status_ts=t1,
        )
        row = (await db_session.execute(
            select(Device.error, Device.error_since).where(Device.id == _device.id)
        )).one()
        assert row.error is None
        assert row.error_since is None

    @pytest.mark.asyncio
    async def test_missing_ssh_enabled_preserves_previous(self, db_session, _device):
        """STATUS without ``ssh_enabled`` must not clear a previously-set value."""
        t0 = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(seconds=30)

        await device_presence.update_status(
            db_session, _device.id, {"ssh_enabled": True}, status_ts=t0,
        )
        # Follow-up STATUS omits the field (older-firmware device) —
        # value should persist.
        await device_presence.update_status(
            db_session, _device.id, {"mode": "play"}, status_ts=t1,
        )
        val = (await db_session.execute(
            select(Device.ssh_enabled).where(Device.id == _device.id)
        )).scalar_one()
        assert val is True


class TestListStates:
    @pytest.mark.asyncio
    async def test_empty_when_no_devices_online(self, db_session, _device):
        states = await device_presence.list_states(db_session)
        assert states == []

    @pytest.mark.asyncio
    async def test_only_online_devices_returned(self, db_session):
        a = Device(id="a", name="A", status=DeviceStatus.ADOPTED)
        b = Device(id="b", name="B", status=DeviceStatus.ADOPTED)
        db_session.add_all([a, b])
        await db_session.commit()
        await device_presence.mark_online(db_session, "a")

        states = await device_presence.list_states(db_session)
        ids = {s["device_id"] for s in states}
        assert ids == {"a"}

    @pytest.mark.asyncio
    async def test_state_shape_contains_ui_contract_keys(self, db_session, _device):
        await device_presence.mark_online(db_session, _device.id)
        await device_presence.update_status(
            db_session, _device.id,
            {"mode": "play", "asset": "x.mp4", "cpu_temp_c": 40.0},
        )
        states = await device_presence.list_states(db_session)
        assert len(states) == 1
        s = states[0]
        for key in (
            "device_id", "mode", "asset", "pipeline_state", "connected_at",
            "cpu_temp_c", "ip_address", "ssh_enabled", "local_api_enabled",
            "display_connected", "display_ports", "error", "error_since", "uptime_seconds",
        ):
            assert key in s, f"missing key: {key}"
        assert s["device_id"] == _device.id
        assert s["cpu_temp_c"] == 40.0

    @pytest.mark.asyncio
    async def test_presence_count_and_ids(self, db_session):
        for did in ("x", "y", "z"):
            db_session.add(Device(id=did, name=did, status=DeviceStatus.ADOPTED))
        await db_session.commit()
        await device_presence.mark_online(db_session, "x")
        await device_presence.mark_online(db_session, "z")

        assert await device_presence.count_online(db_session) == 2
        assert set(await device_presence.ids_online(db_session)) == {"x", "z"}
        assert await device_presence.is_online(db_session, "x") is True
        assert await device_presence.is_online(db_session, "y") is False
        # Unknown id — not a crash
        assert await device_presence.is_online(db_session, "unknown") is False


class TestSetFlags:
    @pytest.mark.asyncio
    async def test_set_flags_updates_allowed_columns(self, db_session, _device):
        await device_presence.set_flags(
            db_session, _device.id, ssh_enabled=True, local_api_enabled=False,
        )
        row = (await db_session.execute(
            select(Device.ssh_enabled, Device.local_api_enabled).where(Device.id == _device.id)
        )).one()
        assert row.ssh_enabled is True
        assert row.local_api_enabled is False

    @pytest.mark.asyncio
    async def test_set_flags_ignores_unknown_columns(self, db_session, _device):
        # Must not raise or touch the row beyond allowed flags
        await device_presence.set_flags(
            db_session, _device.id, ssh_enabled=True, name="hacked",
        )
        row = (await db_session.execute(
            select(Device.name, Device.ssh_enabled).where(Device.id == _device.id)
        )).one()
        assert row.name == "Test Device"
        assert row.ssh_enabled is True


class TestMarkOfflineAndAlert:
    """Issue #406 — CAS-flip presence offline AND fire OFFLINE alert.

    The helper is the single source of truth for "device just dropped"
    side-effects: presence flip + alert dispatch.  These tests pin the
    contract so every transport's send-failure path produces the same
    behaviour.
    """

    @pytest.mark.asyncio
    async def test_cas_hit_flips_offline_and_fires_alert(
        self, db_session, _device, monkeypatch,
    ):
        from cms.services import alert_service as alert_mod

        await device_presence.mark_online(
            db_session, _device.id, connection_id="cid-good",
        )

        calls: list[dict] = []
        monkeypatch.setattr(
            alert_mod.alert_service,
            "device_disconnected",
            lambda did, **kw: calls.append({"device_id": did, **kw}),
        )

        ok = await device_presence.mark_offline_and_alert(
            db_session, _device.id, expected_connection_id="cid-good",
        )

        assert ok is True
        row = (await db_session.execute(
            select(Device.online, Device.connection_id)
            .where(Device.id == _device.id)
        )).one()
        assert row.online is False
        assert row.connection_id is None
        assert len(calls) == 1
        assert calls[0]["device_id"] == _device.id
        assert calls[0]["device_name"] == "Test Device"

    @pytest.mark.asyncio
    async def test_cas_miss_keeps_fresh_session_and_skips_alert(
        self, db_session, _device, monkeypatch,
    ):
        """A stale failure must NOT knock the new session offline.

        Simulates: replica A's send fails after a re-register on
        replica B replaced ``connection_id``.  We pass A's stale token,
        but the row already holds B's fresh token — the CAS misses.
        """
        from cms.services import alert_service as alert_mod

        await device_presence.mark_online(
            db_session, _device.id, connection_id="cid-fresh",
        )

        calls: list[dict] = []
        monkeypatch.setattr(
            alert_mod.alert_service,
            "device_disconnected",
            lambda did, **kw: calls.append({"device_id": did, **kw}),
        )

        ok = await device_presence.mark_offline_and_alert(
            db_session, _device.id, expected_connection_id="cid-stale",
        )

        assert ok is False
        row = (await db_session.execute(
            select(Device.online, Device.connection_id)
            .where(Device.id == _device.id)
        )).one()
        # Fresh session left intact.
        assert row.online is True
        assert row.connection_id == "cid-fresh"
        assert calls == []

    @pytest.mark.asyncio
    async def test_none_token_unconditional_flip_no_alert(
        self, db_session, _device, monkeypatch,
    ):
        """Caller without a trustworthy token: flip best-effort, no alert.

        We fail closed on alerts when we can't tell the stale case from
        the real one, to avoid duplicate OFFLINE notifications.
        """
        from cms.services import alert_service as alert_mod

        await device_presence.mark_online(
            db_session, _device.id, connection_id="cid-1",
        )

        calls: list[dict] = []
        monkeypatch.setattr(
            alert_mod.alert_service,
            "device_disconnected",
            lambda did, **kw: calls.append({"device_id": did, **kw}),
        )

        ok = await device_presence.mark_offline_and_alert(
            db_session, _device.id, expected_connection_id=None,
        )

        assert ok is False  # No alert fired — see docstring.
        row = (await db_session.execute(
            select(Device.online, Device.connection_id)
            .where(Device.id == _device.id)
        )).one()
        assert row.online is False
        assert row.connection_id is None
        assert calls == []

    @pytest.mark.asyncio
    async def test_unknown_device_is_noop(self, db_session, monkeypatch):
        from cms.services import alert_service as alert_mod

        calls: list[dict] = []
        monkeypatch.setattr(
            alert_mod.alert_service,
            "device_disconnected",
            lambda did, **kw: calls.append({"device_id": did, **kw}),
        )

        ok = await device_presence.mark_offline_and_alert(
            db_session, "ghost-device", expected_connection_id="anything",
        )

        assert ok is False
        assert calls == []

    @pytest.mark.asyncio
    async def test_alert_dispatch_failure_does_not_revert_flip(
        self, db_session, _device, monkeypatch,
    ):
        """Alert subsystem blowing up must not unwind the presence flip.

        The presence flip is committed before the alert call, and we
        swallow any alert exception (logged) so a single bad listener
        can't cascade into "device stuck online".
        """
        from cms.services import alert_service as alert_mod

        await device_presence.mark_online(
            db_session, _device.id, connection_id="cid-z",
        )

        def _boom(*a, **kw):
            raise RuntimeError("alerts down")

        monkeypatch.setattr(
            alert_mod.alert_service, "device_disconnected", _boom,
        )

        ok = await device_presence.mark_offline_and_alert(
            db_session, _device.id, expected_connection_id="cid-z",
        )

        assert ok is True  # CAS won — caller view unchanged.
        row = (await db_session.execute(
            select(Device.online, Device.connection_id)
            .where(Device.id == _device.id)
        )).one()
        assert row.online is False
        assert row.connection_id is None
