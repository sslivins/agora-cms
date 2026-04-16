"""Alert service — monitors device health and creates notifications.

Handles two alert types:
  1. Offline detection with configurable grace period
  2. Temperature alerts with hysteresis to prevent flapping

Notifications use scope="group" so only users with access to the device's
group (plus admins via groups:view_all) see them in the bell.
"""

import asyncio
import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional

from cms.models.device_event import DeviceEvent, DeviceEventType
from cms.models.notification import Notification


def _to_uuid(val: str | _uuid.UUID | None) -> _uuid.UUID | None:
    """Coerce a string or UUID to uuid.UUID (needed for UUID columns)."""
    if val is None:
        return None
    return val if isinstance(val, _uuid.UUID) else _uuid.UUID(str(val))

logger = logging.getLogger("agora.cms.alerts")

# Default settings (overridable via CMSSetting)
DEFAULT_OFFLINE_GRACE_SECONDS = 120
DEFAULT_TEMP_WARNING_C = 70.0
DEFAULT_TEMP_CRITICAL_C = 80.0
DEFAULT_TEMP_COOLDOWN_SECONDS = 300


class _TempState:
    """Per-device temperature alert state machine."""

    __slots__ = ("level", "last_alert_at")

    def __init__(self):
        self.level: str = "normal"   # "normal", "warning", "critical"
        self.last_alert_at: Optional[datetime] = None


class _OfflineTimer:
    """Per-device offline grace-period tracker."""

    __slots__ = ("task", "device_name", "group_id", "group_name")

    def __init__(self, task: asyncio.Task, device_name: str,
                 group_id: Optional[str], group_name: str):
        self.task = task
        self.device_name = device_name
        self.group_id = group_id
        self.group_name = group_name


class AlertService:
    """Singleton service wired into the WebSocket handler."""

    def __init__(self):
        self._offline_timers: dict[str, _OfflineTimer] = {}
        self._temp_states: dict[str, _TempState] = {}
        # Tracks devices that had an OFFLINE event fired (for "back online" logic)
        self._was_offline: set[str] = set()
        # Cached settings (refreshed periodically)
        self._offline_grace_seconds: int = DEFAULT_OFFLINE_GRACE_SECONDS
        self._temp_warning_c: float = DEFAULT_TEMP_WARNING_C
        self._temp_critical_c: float = DEFAULT_TEMP_CRITICAL_C
        self._temp_cooldown_seconds: int = DEFAULT_TEMP_COOLDOWN_SECONDS
        self._email_enabled: bool = False

    # ── Settings ──

    async def refresh_settings(self):
        """Reload alert settings from the database."""
        try:
            from cms.database import get_db
            from cms.auth import get_setting
            async for db in get_db():
                val = await get_setting(db, "alert_offline_grace_seconds")
                if val is not None:
                    self._offline_grace_seconds = int(val)
                val = await get_setting(db, "alert_temp_warning_c")
                if val is not None:
                    self._temp_warning_c = float(val)
                val = await get_setting(db, "alert_temp_critical_c")
                if val is not None:
                    self._temp_critical_c = float(val)
                val = await get_setting(db, "alert_temp_cooldown_seconds")
                if val is not None:
                    self._temp_cooldown_seconds = int(val)
                val = await get_setting(db, "email_notifications_enabled")
                self._email_enabled = val == "true"
                break
        except Exception:
            logger.debug("Could not refresh alert settings, using defaults")

    # ── Offline detection ──

    def device_disconnected(
        self,
        device_id: str,
        device_name: str,
        group_id: Optional[str],
        group_name: str,
        status: str,
    ):
        """Called from ws.py when a device's WebSocket closes.

        Only starts a grace timer for adopted devices in a group.
        """
        if status != "adopted" or not group_id:
            return

        # Cancel any existing timer for this device
        existing = self._offline_timers.pop(device_id, None)
        if existing and not existing.task.done():
            existing.task.cancel()

        task = asyncio.create_task(
            self._offline_grace_expired(device_id, device_name, group_id, group_name)
        )
        self._offline_timers[device_id] = _OfflineTimer(
            task=task, device_name=device_name,
            group_id=group_id, group_name=group_name,
        )
        logger.debug(
            "Offline grace timer started for %s (%ds)",
            device_id, self._offline_grace_seconds,
        )

    def device_reconnected(
        self,
        device_id: str,
        device_name: str,
        group_id: Optional[str],
        group_name: str,
        status: str,
    ):
        """Called from ws.py after a successful register.

        Cancels any pending grace timer.  If an OFFLINE event was previously
        fired, creates an ONLINE event + notification.
        """
        # Cancel pending grace timer
        timer = self._offline_timers.pop(device_id, None)
        if timer and not timer.task.done():
            timer.task.cancel()
            logger.debug("Offline grace timer cancelled for %s (reconnected)", device_id)

        # Fire "back online" only if we previously sent an offline notification
        if device_id in self._was_offline:
            self._was_offline.discard(device_id)
            if status == "adopted" and group_id:
                asyncio.create_task(
                    self._create_online_event(device_id, device_name, group_id, group_name)
                )

    async def _offline_grace_expired(
        self,
        device_id: str,
        device_name: str,
        group_id: str,
        group_name: str,
    ):
        """Fires after the grace period if the device hasn't reconnected."""
        try:
            await asyncio.sleep(self._offline_grace_seconds)
        except asyncio.CancelledError:
            return

        # Clean up timer reference
        self._offline_timers.pop(device_id, None)

        # Double-check the device hasn't reconnected during the sleep
        from cms.services.device_manager import device_manager
        if device_manager.is_connected(device_id):
            return

        self._was_offline.add(device_id)
        logger.info("Device %s (%s) offline — grace period expired", device_id, device_name)

        try:
            from cms.database import get_db
            async for db in get_db():
                gid = _to_uuid(group_id)
                event = DeviceEvent(
                    device_id=device_id,
                    device_name=device_name,
                    group_id=gid,
                    group_name=group_name,
                    event_type=DeviceEventType.OFFLINE,
                    details={"grace_seconds": self._offline_grace_seconds},
                )
                db.add(event)

                notification = Notification(
                    scope="group",
                    level="warning",
                    title=f"Device offline: {device_name}",
                    message=(
                        f"Device '{device_name}' in group '{group_name}' has been "
                        f"offline for over {self._offline_grace_seconds} seconds."
                    ),
                    group_id=gid,
                    details={
                        "device_id": device_id,
                        "event_type": "offline",
                    },
                )
                db.add(notification)
                await db.commit()
                logger.info("Offline notification created for device %s", device_id)
                break
        except Exception:
            logger.exception("Failed to create offline event for device %s", device_id)

    async def _create_online_event(
        self,
        device_id: str,
        device_name: str,
        group_id: str,
        group_name: str,
    ):
        """Create a "back online" event after a previous offline alert."""
        try:
            from cms.database import get_db
            gid = _to_uuid(group_id)
            async for db in get_db():
                event = DeviceEvent(
                    device_id=device_id,
                    device_name=device_name,
                    group_id=gid,
                    group_name=group_name,
                    event_type=DeviceEventType.ONLINE,
                )
                db.add(event)

                notification = Notification(
                    scope="group",
                    level="info",
                    title=f"Device back online: {device_name}",
                    message=(
                        f"Device '{device_name}' in group '{group_name}' is back online."
                    ),
                    group_id=gid,
                    details={
                        "device_id": device_id,
                        "event_type": "online",
                    },
                )
                db.add(notification)
                await db.commit()
                logger.info("Online notification created for device %s", device_id)
                break
        except Exception:
            logger.exception("Failed to create online event for device %s", device_id)

    # ── Temperature monitoring ──

    def check_temperature(
        self,
        device_id: str,
        cpu_temp_c: Optional[float],
        device_name: str,
        group_id: Optional[str],
        group_name: str,
        status: str,
    ):
        """Called on every STATUS heartbeat.  Only alerts for adopted+grouped devices."""
        if cpu_temp_c is None or status != "adopted" or not group_id:
            return

        state = self._temp_states.get(device_id)
        if state is None:
            state = _TempState()
            self._temp_states[device_id] = state

        # Determine new level
        if cpu_temp_c >= self._temp_critical_c:
            new_level = "critical"
        elif cpu_temp_c >= self._temp_warning_c:
            new_level = "warning"
        else:
            new_level = "normal"

        if new_level == state.level:
            return  # No transition

        old_level = state.level
        now = datetime.now(timezone.utc)

        # Cooldown: don't re-alert within cooldown after a cleared event
        if (
            old_level == "normal"
            and new_level != "normal"
            and state.last_alert_at
        ):
            elapsed = (now - state.last_alert_at).total_seconds()
            if elapsed < self._temp_cooldown_seconds:
                return

        state.level = new_level

        if new_level == "normal" and old_level != "normal":
            # Temperature cleared
            state.last_alert_at = now
            asyncio.create_task(
                self._create_temp_event(
                    device_id, device_name, group_id, group_name,
                    DeviceEventType.TEMP_CLEARED, cpu_temp_c, old_level,
                )
            )
        elif new_level != "normal":
            # Temperature high (warning or critical)
            state.last_alert_at = now
            asyncio.create_task(
                self._create_temp_event(
                    device_id, device_name, group_id, group_name,
                    DeviceEventType.TEMP_HIGH, cpu_temp_c, new_level,
                )
            )

    async def _create_temp_event(
        self,
        device_id: str,
        device_name: str,
        group_id: str,
        group_name: str,
        event_type: DeviceEventType,
        cpu_temp_c: float,
        level: str,
    ):
        """Create a temperature event + notification."""
        try:
            from cms.database import get_db
            gid = _to_uuid(group_id)
            async for db in get_db():
                event = DeviceEvent(
                    device_id=device_id,
                    device_name=device_name,
                    group_id=gid,
                    group_name=group_name,
                    event_type=event_type,
                    details={
                        "cpu_temp_c": cpu_temp_c,
                        "threshold_warning": self._temp_warning_c,
                        "threshold_critical": self._temp_critical_c,
                        "level": level,
                    },
                )
                db.add(event)

                if event_type == DeviceEventType.TEMP_HIGH:
                    notif_level = "error" if level == "critical" else "warning"
                    title = f"High temperature: {device_name}"
                    message = (
                        f"Device '{device_name}' in group '{group_name}' "
                        f"is at {cpu_temp_c:.1f}°C ({level})."
                    )
                else:
                    notif_level = "info"
                    title = f"Temperature normal: {device_name}"
                    message = (
                        f"Device '{device_name}' in group '{group_name}' "
                        f"temperature returned to normal ({cpu_temp_c:.1f}°C)."
                    )

                notification = Notification(
                    scope="group",
                    level=notif_level,
                    title=title,
                    message=message,
                    group_id=gid,
                    details={
                        "device_id": device_id,
                        "event_type": event_type,
                        "cpu_temp_c": cpu_temp_c,
                    },
                )
                db.add(notification)
                await db.commit()
                logger.info(
                    "Temperature %s notification for device %s (%.1f°C)",
                    event_type, device_id, cpu_temp_c,
                )
                break
        except Exception:
            logger.exception("Failed to create temp event for device %s", device_id)

    # ── Cleanup ──

    def cleanup_device(self, device_id: str):
        """Remove all in-memory state for a device (e.g. when deleted)."""
        timer = self._offline_timers.pop(device_id, None)
        if timer and not timer.task.done():
            timer.task.cancel()
        self._temp_states.pop(device_id, None)
        self._was_offline.discard(device_id)


# Singleton
alert_service = AlertService()
