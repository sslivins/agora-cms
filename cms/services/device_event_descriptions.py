"""Human-readable descriptions for device events.

Single source of truth used by both:

* ``cms/templates/event_log.html`` (via :func:`build_event_description`
  populated onto each ORM event in ``cms/ui.py::event_log_page``)
* ``/api/device-events`` (via :class:`cms.schemas.device_event.DeviceEventOut`
  populated in ``cms/routers/device_events.py``)

The auto-refresh polling JS on the event log page also uses the
description from the API response so server-rendered rows and
freshly-polled rows look identical.

Mirrors the pattern of ``cms/services/audit_service.py::build_description``.
"""

from __future__ import annotations

from cms.models.device_event import DeviceEventType


# Friendly UI label for each event type — used both for the badge text
# on the event-log page and for the filter dropdown options. New enum
# values that aren't in this map fall back to a titlecased version of
# the raw enum value (e.g. ``ota_download_progress`` → ``Ota Download
# Progress``); add an entry below to override.
EVENT_TYPE_LABELS: dict[str, str] = {
    DeviceEventType.ONLINE.value:                  "Online",
    DeviceEventType.OFFLINE.value:                 "Offline",
    DeviceEventType.TEMP_HIGH.value:               "Temp High",
    DeviceEventType.TEMP_CLEARED.value:            "Temp Cleared",
    DeviceEventType.DISPLAY_CONNECTED.value:       "Display Connected",
    DeviceEventType.DISPLAY_DISCONNECTED.value:    "Display Disconnected",
    DeviceEventType.ERROR.value:                   "Error",
    DeviceEventType.ERROR_CLEARED.value:           "Error Cleared",
    DeviceEventType.CMS_STARTED.value:             "CMS Started",
    DeviceEventType.CMS_STOPPED.value:             "CMS Stopped",
    # OTA event badge labels use "Upgrade" terminology to match the
    # "Upgrade" button users click in the device page UI.  Internal
    # event_type strings still say ``ota_*`` (those are the protocol /
    # storage layer), but the visible UI label says "Upgrade".
    DeviceEventType.OTA_DOWNLOAD_STARTED.value:    "Upgrade Started",
    DeviceEventType.OTA_DOWNLOAD_PROGRESS.value:   "Upgrade Downloading",
    DeviceEventType.OTA_SIGNATURE_VERIFIED.value:  "Upgrade Verified",
    DeviceEventType.OTA_STAGED.value:              "Upgrade Staged",
    DeviceEventType.OTA_STAGE_PROGRESS.value:      "Upgrade Staging",
    DeviceEventType.OTA_EXTRACT_PROGRESS.value:    "Upgrade Extracting",
    DeviceEventType.OTA_TRYBOOT_INITIATED.value:   "Upgrade Rebooting",
    DeviceEventType.OTA_SLOT_CONFIRMED.value:      "Upgrade Slot Confirmed",
    DeviceEventType.OTA_PROMOTED.value:            "Upgrade Promoted",
    DeviceEventType.OTA_MIGRATION_COMPLETE.value:  "Upgrade Complete",
    DeviceEventType.OTA_FAILED.value:              "Upgrade Failed",
    DeviceEventType.OTA_DECLINED.value:            "Upgrade Declined",
    DeviceEventType.OTA_AUTO_CLEARED.value:        "Upgrade Cleared",
}


# Badge CSS class per event type.  Keep in sync with the badge palette
# in ``cms/static/css/`` — anything not listed falls back to
# ``badge-muted`` (grey).
EVENT_TYPE_BADGE: dict[str, str] = {
    DeviceEventType.ONLINE.value:                  "badge-online",
    DeviceEventType.OFFLINE.value:                 "badge-offline",
    DeviceEventType.TEMP_HIGH.value:               "badge-warning",
    DeviceEventType.TEMP_CLEARED.value:            "badge-success",
    DeviceEventType.DISPLAY_CONNECTED.value:       "badge-online",
    DeviceEventType.DISPLAY_DISCONNECTED.value:    "badge-offline",
    DeviceEventType.ERROR.value:                   "badge-danger",
    DeviceEventType.ERROR_CLEARED.value:           "badge-success",
    DeviceEventType.CMS_STARTED.value:             "badge-online",
    DeviceEventType.CMS_STOPPED.value:             "badge-offline",
    DeviceEventType.OTA_DOWNLOAD_STARTED.value:    "badge-info",
    DeviceEventType.OTA_DOWNLOAD_PROGRESS.value:   "badge-info",
    DeviceEventType.OTA_SIGNATURE_VERIFIED.value:  "badge-info",
    DeviceEventType.OTA_STAGED.value:              "badge-info",
    DeviceEventType.OTA_STAGE_PROGRESS.value:      "badge-info",
    DeviceEventType.OTA_EXTRACT_PROGRESS.value:    "badge-info",
    DeviceEventType.OTA_TRYBOOT_INITIATED.value:   "badge-info",
    DeviceEventType.OTA_SLOT_CONFIRMED.value:      "badge-info",
    DeviceEventType.OTA_PROMOTED.value:            "badge-success",
    DeviceEventType.OTA_MIGRATION_COMPLETE.value:  "badge-success",
    DeviceEventType.OTA_FAILED.value:              "badge-danger",
    DeviceEventType.OTA_DECLINED.value:            "badge-warning",
    DeviceEventType.OTA_AUTO_CLEARED.value:        "badge-muted",
}


def event_type_label(event_type: str) -> str:
    """Return the friendly badge/dropdown label for an event type.

    Falls back to a titlecased version of the raw enum value for any
    unmapped event type — guarantees the UI never shows raw
    ``snake_case`` even when a new enum value ships before this map is
    updated.
    """
    if event_type in EVENT_TYPE_LABELS:
        return EVENT_TYPE_LABELS[event_type]
    return event_type.replace("_", " ").title()


def event_type_badge_class(event_type: str) -> str:
    """Return the CSS badge class for an event type."""
    return EVENT_TYPE_BADGE.get(event_type, "badge-muted")


def _ota_version(d: dict) -> str:
    """Best-effort version label for an OTA event payload.

    ``target_version`` is the canonical field written by
    ``cms/services/device_inbound.py``.  Falls back to ``release_id``
    when the device omitted the version (older agora firmware).
    """
    return d.get("target_version") or d.get("release_id") or ""


def _ota_pct(payload: dict) -> str:
    """Render ``payload.bytes_done`` / ``bytes_total`` as a percentage."""
    done = payload.get("bytes_done")
    total = payload.get("bytes_total")
    try:
        if total and isinstance(done, (int, float)) and isinstance(total, (int, float)) and total > 0:
            return f"{(float(done) / float(total)) * 100:.0f}%"
    except (TypeError, ValueError):
        pass
    return ""


def build_event_description(event_type: str, details: dict | None = None) -> str:
    """Build a human-readable summary for one device event.

    Never returns raw JSON: an unmapped ``event_type`` falls back to
    its titlecased label so the Details column always shows clean
    text.  Power users can click the row to expand the raw JSON
    drawer for forensic detail.
    """
    d = details or {}

    if event_type == DeviceEventType.ONLINE.value:
        return "Back online"

    if event_type == DeviceEventType.OFFLINE.value:
        kind = d.get("kind")
        if kind == "stale_heartbeat":
            return "No heartbeat received within timeout"
        if kind == "grace_expired":
            return "Grace period exceeded — device declared offline"
        if d.get("grace_period") is not None:
            return f"Grace period: {d['grace_period']}s"
        return "Device went offline"

    if event_type in (DeviceEventType.TEMP_HIGH.value, DeviceEventType.TEMP_CLEARED.value):
        temp = d.get("temperature", "?")
        threshold = d.get("threshold", "?")
        return f"{temp}°C (threshold: {threshold}°C)"

    if event_type == DeviceEventType.DISPLAY_CONNECTED.value:
        name = d.get("display_name") or d.get("name")
        return f"Display connected: {name}" if name else "Display connected"

    if event_type == DeviceEventType.DISPLAY_DISCONNECTED.value:
        name = d.get("display_name") or d.get("name")
        return f"Display disconnected: {name}" if name else "Display disconnected"

    if event_type == DeviceEventType.ERROR.value:
        msg = d.get("message") or d.get("error") or d.get("reason")
        return f"Error: {msg}" if msg else "Error reported"

    if event_type == DeviceEventType.ERROR_CLEARED.value:
        msg = d.get("message") or d.get("error") or d.get("reason")
        return f"Error cleared: {msg}" if msg else "Error cleared"

    if event_type in (DeviceEventType.CMS_STARTED.value, DeviceEventType.CMS_STOPPED.value):
        version = d.get("version")
        replica = d.get("replica_id")
        verb = "started" if event_type == DeviceEventType.CMS_STARTED.value else "stopped"
        if version and replica:
            return f"CMS {verb} — version {version} (replica {replica})"
        if version:
            return f"CMS {verb} — version {version}"
        return f"CMS {verb}"

    # ── OTA events ────────────────────────────────────────────────
    # Payload structure (from cms/services/device_inbound.py:601):
    #   details = {
    #     event_id, payload (dict, opaque), release_id,
    #     target_version, occurred_at, reason, projection_applied,
    #   }
    # The nested ``payload`` is the wire-format dict from the device
    # and is exactly what previously exploded the column width.
    payload = d.get("payload") or {}
    version = _ota_version(d)
    suffix = f" — {version}" if version else ""

    if event_type == DeviceEventType.OTA_DOWNLOAD_STARTED.value:
        return f"Upgrade started{suffix}"

    if event_type == DeviceEventType.OTA_DOWNLOAD_PROGRESS.value:
        pct = _ota_pct(payload)
        if pct:
            return f"Downloading upgrade{suffix}: {pct}"
        return f"Downloading upgrade{suffix}"

    if event_type == DeviceEventType.OTA_SIGNATURE_VERIFIED.value:
        return f"Upgrade signature verified{suffix}"

    if event_type == DeviceEventType.OTA_STAGED.value:
        return f"Upgrade staged{suffix}"

    if event_type == DeviceEventType.OTA_STAGE_PROGRESS.value:
        phase = payload.get("phase")
        if phase:
            return f"Staging upgrade{suffix} ({phase.replace('_', ' ')})"
        return f"Staging upgrade{suffix}"

    if event_type == DeviceEventType.OTA_EXTRACT_PROGRESS.value:
        pct = _ota_pct(payload)
        if pct:
            return f"Extracting upgrade{suffix}: {pct}"
        return f"Extracting upgrade{suffix}"

    if event_type == DeviceEventType.OTA_TRYBOOT_INITIATED.value:
        return f"Rebooting for upgrade{suffix}"

    if event_type == DeviceEventType.OTA_SLOT_CONFIRMED.value:
        return f"Upgrade slot confirmed{suffix}"

    if event_type == DeviceEventType.OTA_PROMOTED.value:
        return f"Upgrade promoted{suffix}"

    if event_type == DeviceEventType.OTA_MIGRATION_COMPLETE.value:
        return f"Upgrade complete{suffix}"

    if event_type == DeviceEventType.OTA_FAILED.value:
        reason = d.get("reason") or payload.get("reason") or payload.get("error")
        if reason and version:
            return f"Upgrade failed{suffix} — {reason}"
        if reason:
            return f"Upgrade failed — {reason}"
        return f"Upgrade failed{suffix}" if version else "Upgrade failed"

    if event_type == DeviceEventType.OTA_DECLINED.value:
        reason = d.get("reason") or payload.get("reason")
        if reason:
            return f"Upgrade declined{suffix} — {reason}"
        return f"Upgrade declined{suffix}" if version else "Upgrade declined"

    if event_type == DeviceEventType.OTA_AUTO_CLEARED.value:
        return f"Upgrade state cleared{suffix}"

    # Unknown event type — never dump raw JSON.  The titlecased label
    # is always at least readable; ops can click the row to see the
    # raw payload if they need details.
    return event_type_label(event_type)
