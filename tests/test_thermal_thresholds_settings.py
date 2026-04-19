"""Coverage for DB-settings-driven thermal thresholds (#309).

``alert_service._temp_warning_c`` / ``_temp_critical_c`` /
``_temp_cooldown_seconds`` can all be overridden via the DB
``alert_*`` settings and reloaded via ``refresh_settings()``.

``test_alert_settings_api.py`` verifies that saving settings persists
them and that ``refresh_settings()`` reads the right values back into
the service. What was missing: proof that those DB-driven thresholds
*actually affect classification* — i.e. that a non-default warning
threshold of 60°C really does classify 61°C as "warning" (when the
default threshold of 70°C wouldn't).

This file fills that gap.
"""

from __future__ import annotations

import pytest

from cms.services.alert_service import AlertService

# ``check_temperature`` schedules follow-up work via ``asyncio.create_task``,
# which needs a running event loop. Mark every test async so pytest-asyncio
# provides one. We don't ``await`` the spawned tasks — they hit the DB and
# we only care about the classification side effect on ``_temp_states``.
pytestmark = pytest.mark.asyncio


def _svc(*, warning: float, critical: float, cooldown: int = 300) -> AlertService:
    svc = AlertService()
    svc._temp_warning_c = warning
    svc._temp_critical_c = critical
    svc._temp_cooldown_seconds = cooldown
    return svc


def _check(svc: AlertService, device_id: str, temp: float) -> str | None:
    """Fire a check and return the resulting state.level (None if no state)."""
    svc.check_temperature(
        device_id=device_id,
        cpu_temp_c=temp,
        device_name="dev-1",
        group_id="11111111-1111-1111-1111-111111111111",
        group_name="Group 1",
        status="adopted",
    )
    state = svc._temp_states.get(device_id)
    return state.level if state else None


class TestCustomWarningThreshold:
    async def test_warning_at_low_threshold(self):
        """At warning=60°C, 61°C classifies as warning."""
        svc = _svc(warning=60.0, critical=90.0)
        assert _check(svc, "dev-low", 61.0) == "warning"

    async def test_warning_at_high_threshold(self):
        """At warning=85°C, 80°C is still normal (default would be warning)."""
        svc = _svc(warning=85.0, critical=95.0)
        level = _check(svc, "dev-high", 80.0)
        # Normal on first call leaves state.level == "normal" (the dataclass default)
        # and does not queue an event. We just assert it was NOT classified as warning.
        assert level != "warning", (
            f"80°C with warning threshold 85°C should not be warning; got {level}"
        )

    async def test_critical_at_low_threshold(self):
        """At critical=70°C, 75°C classifies as critical."""
        svc = _svc(warning=60.0, critical=70.0)
        assert _check(svc, "dev-crit", 75.0) == "critical"

    async def test_boundary_exactly_at_warning(self):
        """Temperature exactly at warning threshold classifies as warning (>=)."""
        svc = _svc(warning=65.0, critical=85.0)
        assert _check(svc, "dev-boundary-w", 65.0) == "warning"

    async def test_boundary_exactly_at_critical(self):
        """Temperature exactly at critical threshold classifies as critical (>=)."""
        svc = _svc(warning=65.0, critical=85.0)
        assert _check(svc, "dev-boundary-c", 85.0) == "critical"

    async def test_just_below_custom_warning_stays_normal(self):
        """Just under the custom warning threshold stays normal."""
        svc = _svc(warning=50.0, critical=70.0)
        level = _check(svc, "dev-just-below", 49.9)
        assert level in (None, "normal")


class TestThresholdChangeDuringLifetime:
    """Re-reading settings mid-run should change classification on next check."""

    async def test_lowering_warning_triggers_new_warning(self):
        svc = _svc(warning=80.0, critical=95.0)
        assert _check(svc, "dev-change", 75.0) in (None, "normal")
        svc._temp_warning_c = 70.0
        assert _check(svc, "dev-change", 75.0) == "warning"

    async def test_raising_warning_clears_previous_warning(self):
        svc = _svc(warning=60.0, critical=90.0)
        assert _check(svc, "dev-raise", 70.0) == "warning"
        svc._temp_warning_c = 80.0
        assert _check(svc, "dev-raise", 70.0) == "normal"


class TestCustomCooldown:
    """After clearing, a new alert should respect the DB-driven cooldown seconds."""

    async def test_cooldown_value_is_respected(self):
        """Short cooldown = next alert after clear should fire on next heartbeat."""
        import datetime as _dt

        svc = _svc(warning=60.0, critical=90.0, cooldown=1)
        assert _check(svc, "dev-cooldown", 70.0) == "warning"
        assert _check(svc, "dev-cooldown", 50.0) == "normal"
        state = svc._temp_states["dev-cooldown"]
        state.last_alert_at = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=2)
        assert _check(svc, "dev-cooldown", 75.0) == "warning"


async def test_refresh_settings_changes_classification(app, db_session):
    """End-to-end: save settings via ``set_setting``, refresh_settings(), classify.

    Documents that the full DB-read loop drives threshold behaviour.
    """
    from cms.auth import set_setting

    await set_setting(db_session, "alert_temp_warning_c", "55.0")
    await set_setting(db_session, "alert_temp_critical_c", "95.0")
    await set_setting(db_session, "alert_temp_cooldown_seconds", "10")
    await db_session.commit()

    svc = AlertService()
    # Default before refresh
    assert svc._temp_warning_c == 70.0

    await svc.refresh_settings()
    assert svc._temp_warning_c == 55.0
    assert svc._temp_critical_c == 95.0
    assert svc._temp_cooldown_seconds == 10

    # Behavior: 60°C with new 55° threshold should warn (would be normal under defaults).
    level = _check(svc, "dev-e2e", 60.0)
    assert level == "warning"
