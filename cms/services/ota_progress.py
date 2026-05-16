"""OTA progress projection — lifecycle events → ``devices.ota_*`` columns.

Issue agora-cms#574 (companion to ``sslivins/agora#215``).  Devices on
firmware that ships the WPS lifecycle event sender (see agora#215) push
a stream of ``lifecycle_event`` messages over WPS during every OTA.
This module is the pure projection layer: it takes one wire event +
payload, applies the regression-safe state transitions, and mutates a
``Device`` ORM row in place.  The caller (`device_inbound.py`) owns the
commit.

The data model is **multi-replica safe by virtue of Postgres**: every
event causes a single ``UPDATE devices SET ota_* = ...`` so any replica
reading the row in a subsequent transaction sees the latest applied
event.  No in-memory cache — the bug we explicitly avoid is one
replica's webhook updating an in-memory dict while a different replica
serves ``/api/devices`` and reads NULLs.

Three correctness rails:

1. **Phase-order regression guard.**  ``_PHASE_ORDER`` assigns each
   known event_type an integer.  An incoming event whose order is
   strictly less than the row's current order is dropped.  Same-order
   download/extract events check ``bytes_done`` for monotonicity.

2. **Explicit clearing on terminal events.**  Promoted, migration-
   complete, failed, and declined all clear every ``ota_*`` column to
   NULL *and* drop the ``upgrade_started_at`` claim so the badge falls
   off immediately rather than waiting for ``UPGRADE_TTL`` to expire.

3. **pct=None on phase transitions to non-progress phases.**  When the
   wire event carries no bytes (signature_verified, tryboot_initiated,
   slot_confirmed, …) the projection writes ``pct = None`` rather than
   leaving the previous phase's 100% sticking around.

Forward-compat: an unknown ``event_type`` (something a newer firmware
ships that this CMS hasn't been taught about yet) is a no-op — the
caller has already logged + dropped before we'd be called.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cms.models.device import Device


# ── Phase ordering ─────────────────────────────────────────────────────
#
# Smaller integer = earlier in the OTA timeline.  We use a single
# ordinal for the two "during one of the long staging phases" events
# (stage_progress + extract_progress) because their CMS-side ordering
# relative to each other isn't well-defined — extract_progress fires
# *during* the extract sub-phases of stage_progress, and the timestamps
# on the wire don't quite align.  Treating them as the same step lets
# us still reject e.g. a stale ``staged`` event arriving after the
# device already moved on to ``tryboot_initiated``.
_PHASE_ORDER: dict[str, int] = {
    "ota_download_started":      10,
    "ota_download_progress":     20,
    "ota_signature_verified":    30,
    "ota_staged":                40,
    "ota_stage_progress":        50,
    "ota_extract_progress":      50,
    "ota_tryboot_initiated":     70,
    "ota_slot_confirmed":        80,
    "ota_promoted":              90,
    "ota_migration_complete":   100,
    "ota_failed":               999,
    "ota_declined":             999,
}

_TERMINAL: frozenset[str] = frozenset(
    {"ota_promoted", "ota_migration_complete", "ota_failed", "ota_declined"}
)

# Stable label strings for the UI.  The bare ``ota_phase`` is also kept
# on the row so the front end can branch on it (a tooltip, a colour, an
# icon — all easier off a stable token than a freeform label).
_PHASE_LABELS: dict[str, str] = {
    "ota_download_started":     "Starting download",
    "ota_download_progress":    "Downloading bundle",
    "ota_signature_verified":   "Verifying signature",
    "ota_staged":               "Staging bundle",
    "ota_stage_progress":       "Staging",
    "ota_extract_progress":     "Extracting bundle",
    "ota_tryboot_initiated":    "Rebooting into new slot",
    "ota_slot_confirmed":       "Confirming new slot",
    "ota_promoted":             "Promoted",
    "ota_migration_complete":   "Promoted",
    "ota_failed":               "Failed",
    "ota_declined":             "Declined",
}

# stage_progress sub-phase → human label.  These come through as the
# ``phase`` field of the payload (one of the 8 entries in
# ``agora/os_updater/apply.py::STAGE_PROGRESS_PHASES``).
_STAGE_SUBPHASE_LABELS: dict[str, str] = {
    "extracting_meta":     "Extracting metadata",
    "mounting_inactive":   "Mounting inactive slot",
    "wiping_inactive":     "Wiping inactive slot",
    "extracting_boot":     "Extracting boot",
    "extracting_rootfs":   "Extracting rootfs",
    "verifying_manifest":  "Verifying manifest",
    "copying_fleet_state": "Copying fleet state",
    "finalizing":          "Finalizing",
}

# extract_progress sub-phase → label.  This event fires *during* either
# the extracting_boot or extracting_rootfs stage_progress windows, and
# carries bytes_done / bytes_total of the underlying zstd stream.
_EXTRACT_SUBPHASE_LABELS: dict[str, str] = {
    "boot":   "Extracting boot",
    "rootfs": "Extracting rootfs",
}


def _derive(event_type: str, payload: dict) -> tuple[
    str, str, float | None, int | None, int | None,
]:
    """Pure event → (phase_token, label, pct, bytes_done, bytes_total).

    Caller has already validated ``event_type`` is in ``_PHASE_ORDER``.
    Payload schema by event:

      - download_progress: ``{bytes_done, bytes_total}``
      - stage_progress:    ``{phase: <one of 8 sub-phases>}``
      - extract_progress:  ``{phase: "boot"|"rootfs", bytes_done, bytes_total}``
      - all others:        no body fields used here
    """
    phase = event_type
    label = _PHASE_LABELS.get(event_type, event_type)
    pct: float | None = None
    bytes_done: int | None = None
    bytes_total: int | None = None

    if event_type == "ota_download_progress":
        bytes_done = _safe_int(payload.get("bytes_done"))
        bytes_total = _safe_int(payload.get("bytes_total"))
        pct = _safe_pct(bytes_done, bytes_total)

    elif event_type == "ota_stage_progress":
        sub = payload.get("phase")
        if isinstance(sub, str) and sub in _STAGE_SUBPHASE_LABELS:
            label = _STAGE_SUBPHASE_LABELS[sub]
        # stage_progress carries no bytes — leave pct / bytes None so a
        # transition out of download (which had pct=100) doesn't visually
        # stick at "100% complete" through the staging window.

    elif event_type == "ota_extract_progress":
        sub = payload.get("phase")
        if isinstance(sub, str) and sub in _EXTRACT_SUBPHASE_LABELS:
            label = _EXTRACT_SUBPHASE_LABELS[sub]
        bytes_done = _safe_int(payload.get("bytes_done"))
        bytes_total = _safe_int(payload.get("bytes_total"))
        pct = _safe_pct(bytes_done, bytes_total)

    # All other event_types (download_started, signature_verified,
    # staged, tryboot_initiated, slot_confirmed) carry no progress —
    # the defaults above already give them label + None pct/bytes.

    return phase, label, pct, bytes_done, bytes_total


def _safe_int(v) -> int | None:
    if v is None:
        return None
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if i >= 0 else None


def _safe_pct(done: int | None, total: int | None) -> float | None:
    if done is None or total is None or total <= 0:
        return None
    pct = (done / total) * 100.0
    if pct < 0.0:
        return 0.0
    if pct > 100.0:
        return 100.0
    return pct


def handle_event(device: "Device", event_type: str, payload: dict) -> bool:
    """Apply one lifecycle event to ``device``.  Returns True if mutated.

    Caller owns the ``await db.commit()``.  Returns False on
    forward-compat unknowns and on regression-guard drops so the caller
    can still emit a ``DeviceEvent`` audit row even when the projection
    is a no-op (the device's monotonic ``event_id`` still counted that
    event, regression-dropped or not — we don't want gaps in the audit
    log).
    """
    new_order = _PHASE_ORDER.get(event_type)
    if new_order is None:
        return False

    cur_phase = device.ota_phase
    cur_order = _PHASE_ORDER.get(cur_phase, -1) if cur_phase else -1

    # Regression guard #1: an older phase event arriving after a newer
    # one (network reorder, replay, etc.) is dropped.  Terminal events
    # (order=999) are explicitly allowed even from earlier phases —
    # they're how the device tells us the OTA ended.
    if new_order < cur_order and event_type not in _TERMINAL:
        return False

    if event_type in _TERMINAL:
        device.ota_phase = None
        device.ota_label = None
        device.ota_pct = None
        device.ota_bytes_done = None
        device.ota_bytes_total = None
        device.ota_updated_at = None
        # Release the atomic upgrade claim immediately.  This is what
        # makes the badge fall off the moment ``promoted`` arrives,
        # rather than waiting for the device to reboot + re-register
        # (success path) or for ``UPGRADE_TTL`` to expire (failure
        # path).  Today the register-handler clears it on a real
        # version bump too; this is the belt-and-suspenders that
        # closes the failure-path UX gap (badge was sticking
        # "Upgrading…" for 15 minutes after every failed OTA).
        device.upgrade_started_at = None
        return True

    phase, label, pct, bytes_done, bytes_total = _derive(event_type, payload or {})

    # Regression guard #2: same phase, byte counter went backwards.
    # Catches a rebroadcast of an older download_progress event after
    # a newer one already landed.
    if new_order == cur_order and bytes_done is not None:
        prev_bytes_done = device.ota_bytes_done
        if prev_bytes_done is not None and bytes_done < prev_bytes_done:
            return False

    device.ota_phase = phase
    device.ota_label = label
    device.ota_pct = pct
    device.ota_bytes_done = bytes_done
    device.ota_bytes_total = bytes_total
    device.ota_updated_at = datetime.now(timezone.utc)
    return True
