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

This file fills that gap by exercising the pure-function
``_classify_temp`` classifier directly; the full DB-backed
``check_temperature`` path is covered by ``test_device_alerts.py``.
"""

from __future__ import annotations

import pytest

from cms.services.alert_service import AlertService


def _svc(*, warning: float, critical: float, cooldown: int = 300) -> AlertService:
    svc = AlertService()
    svc._temp_warning_c = warning
    svc._temp_critical_c = critical
    svc._temp_cooldown_seconds = cooldown
    return svc


class TestCustomWarningThreshold:
    def test_warning_at_low_threshold(self):
        """At warning=60°C, 61°C classifies as warning."""
        svc = _svc(warning=60.0, critical=90.0)
        assert svc._classify_temp(61.0) == "warning"

    def test_warning_at_high_threshold(self):
        """At warning=85°C, 80°C is still normal (default would be warning)."""
        svc = _svc(warning=85.0, critical=95.0)
        assert svc._classify_temp(80.0) == "normal"

    def test_critical_at_low_threshold(self):
        """At critical=70°C, 75°C classifies as critical."""
        svc = _svc(warning=60.0, critical=70.0)
        assert svc._classify_temp(75.0) == "critical"

    def test_boundary_exactly_at_warning(self):
        """Temperature exactly at warning threshold classifies as warning (>=)."""
        svc = _svc(warning=65.0, critical=85.0)
        assert svc._classify_temp(65.0) == "warning"

    def test_boundary_exactly_at_critical(self):
        """Temperature exactly at critical threshold classifies as critical (>=)."""
        svc = _svc(warning=65.0, critical=85.0)
        assert svc._classify_temp(85.0) == "critical"

    def test_just_below_custom_warning_stays_normal(self):
        """Just under the custom warning threshold stays normal."""
        svc = _svc(warning=50.0, critical=70.0)
        assert svc._classify_temp(49.9) == "normal"


class TestThresholdChangeDuringLifetime:
    """Re-reading settings mid-run should change classification on next check."""

    def test_lowering_warning_triggers_new_warning(self):
        svc = _svc(warning=80.0, critical=95.0)
        assert svc._classify_temp(75.0) == "normal"
        svc._temp_warning_c = 70.0
        assert svc._classify_temp(75.0) == "warning"

    def test_raising_warning_clears_previous_warning(self):
        svc = _svc(warning=60.0, critical=90.0)
        assert svc._classify_temp(70.0) == "warning"
        svc._temp_warning_c = 80.0
        assert svc._classify_temp(70.0) == "normal"


@pytest.mark.asyncio
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
    assert svc._classify_temp(60.0) == "warning"
