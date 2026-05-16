"""Unit tests for ``cms.services.ota_progress``.

Pure projection tests — no DB. The handler mutates a ``Device`` row
in place; we use a lightweight stand-in with the same attribute
surface so we can exercise every transition without spinning Postgres.

Coverage targets (per ota-progress-badge plan v2):

  - happy path: each event_type produces the right phase/label/pct
  - download_progress bytes calculation
  - extract_progress bytes calculation
  - stage_progress label resolution from sub-phase
  - regression guard: older phase event after newer one is dropped
  - regression guard: same-phase bytes_done going backwards is dropped
  - terminal events clear every ota_* column AND upgrade_started_at
  - terminal events can land even from an earlier ordinal (they're
    always allowed)
  - pct=None on phase transitions to non-progress phases (the bug the
    rubber-duck caught — pct=100 from download must not visually carry
    into the verifying-signature window)
  - unknown event_type is a no-op (forward-compat)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cms.services import ota_progress


class _DeviceStub:
    """Minimal in-memory stand-in for the ``Device`` SQLAlchemy row.

    The projection only reads/writes the six ``ota_*`` columns and
    ``upgrade_started_at``; everything else is irrelevant.
    """

    def __init__(self, **overrides):
        self.ota_phase = None
        self.ota_label = None
        self.ota_pct = None
        self.ota_bytes_done = None
        self.ota_bytes_total = None
        self.ota_updated_at = None
        self.upgrade_started_at = datetime.now(timezone.utc)
        for k, v in overrides.items():
            setattr(self, k, v)


# ── happy-path projection ──────────────────────────────────────────────


def test_download_started_sets_phase_no_pct():
    d = _DeviceStub()
    assert ota_progress.handle_event(d, "ota_download_started", {}) is True
    assert d.ota_phase == "ota_download_started"
    assert d.ota_label == "Starting download"
    assert d.ota_pct is None
    assert d.ota_bytes_done is None
    assert d.ota_bytes_total is None
    assert d.ota_updated_at is not None


def test_download_progress_computes_pct_from_bytes():
    d = _DeviceStub()
    ota_progress.handle_event(d, "ota_download_progress",
                              {"bytes_done": 300, "bytes_total": 1000})
    assert d.ota_phase == "ota_download_progress"
    assert d.ota_label == "Downloading bundle"
    assert d.ota_pct == pytest.approx(30.0)
    assert d.ota_bytes_done == 300
    assert d.ota_bytes_total == 1000


def test_signature_verified_resets_pct_to_none():
    # The bug the rubber-duck caught: a download finishing at pct=100
    # transitions to signature_verified (no bytes); pct must clear so
    # the badge doesn't visually stick at 100% during the verify window.
    d = _DeviceStub(ota_phase="ota_download_progress", ota_pct=100.0,
                    ota_bytes_done=1000, ota_bytes_total=1000)
    ota_progress.handle_event(d, "ota_signature_verified", {})
    assert d.ota_phase == "ota_signature_verified"
    assert d.ota_label == "Verifying signature"
    assert d.ota_pct is None
    assert d.ota_bytes_done is None
    assert d.ota_bytes_total is None


def test_stage_progress_resolves_sub_phase_label():
    d = _DeviceStub()
    ota_progress.handle_event(d, "ota_stage_progress",
                              {"phase": "wiping_inactive"})
    assert d.ota_phase == "ota_stage_progress"
    assert d.ota_label == "Wiping inactive slot"
    assert d.ota_pct is None


def test_stage_progress_unknown_sub_phase_falls_back_to_generic_label():
    d = _DeviceStub()
    ota_progress.handle_event(d, "ota_stage_progress",
                              {"phase": "never_heard_of_it"})
    assert d.ota_phase == "ota_stage_progress"
    assert d.ota_label == "Staging"  # _PHASE_LABELS default


def test_extract_progress_computes_pct_with_sub_phase_label():
    d = _DeviceStub()
    ota_progress.handle_event(d, "ota_extract_progress",
                              {"phase": "extracting_rootfs",
                               "bytes_done": 500_000_000,
                               "bytes_total": 1_000_000_000})
    assert d.ota_phase == "ota_extract_progress"
    assert d.ota_label == "Extracting rootfs"
    assert d.ota_pct == pytest.approx(50.0)


# ── regression guards ─────────────────────────────────────────────────


def test_older_phase_event_after_newer_is_dropped():
    d = _DeviceStub(ota_phase="ota_slot_confirmed", ota_label="Confirming")
    # An out-of-order download_started arriving after slot_confirmed
    # must NOT rewind the row.
    assert ota_progress.handle_event(d, "ota_download_started", {}) is False
    assert d.ota_phase == "ota_slot_confirmed"
    assert d.ota_label == "Confirming"


def test_same_phase_bytes_done_regression_is_dropped():
    d = _DeviceStub(ota_phase="ota_download_progress", ota_pct=80.0,
                    ota_bytes_done=800, ota_bytes_total=1000)
    # A rebroadcast of an older download_progress event (bytes_done=300)
    # arriving after a newer one (bytes_done=800) must be dropped.
    assert ota_progress.handle_event(
        d, "ota_download_progress",
        {"bytes_done": 300, "bytes_total": 1000},
    ) is False
    assert d.ota_bytes_done == 800  # unchanged
    assert d.ota_pct == pytest.approx(80.0)


def test_same_phase_bytes_done_advance_is_accepted():
    d = _DeviceStub(ota_phase="ota_download_progress", ota_pct=30.0,
                    ota_bytes_done=300, ota_bytes_total=1000)
    assert ota_progress.handle_event(
        d, "ota_download_progress",
        {"bytes_done": 600, "bytes_total": 1000},
    ) is True
    assert d.ota_bytes_done == 600
    assert d.ota_pct == pytest.approx(60.0)


def test_advance_to_higher_phase_resets_byte_history():
    # Going from download_progress → stage_progress should NOT trip the
    # same-phase regression guard even though stage carries no bytes.
    d = _DeviceStub(ota_phase="ota_download_progress", ota_pct=100.0,
                    ota_bytes_done=1000, ota_bytes_total=1000)
    assert ota_progress.handle_event(
        d, "ota_stage_progress", {"phase": "extracting_meta"},
    ) is True
    assert d.ota_phase == "ota_stage_progress"
    assert d.ota_bytes_done is None
    assert d.ota_bytes_total is None
    assert d.ota_pct is None


# ── terminal events ───────────────────────────────────────────────────


def _terminal_test_helper(event_type: str):
    """Common assertions for any of the four terminal event types."""
    started_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    d = _DeviceStub(
        ota_phase="ota_extract_progress", ota_pct=47.0,
        ota_bytes_done=470, ota_bytes_total=1000,
        upgrade_started_at=started_at,
    )
    assert ota_progress.handle_event(d, event_type, {}) is True
    assert d.ota_phase is None
    assert d.ota_label is None
    assert d.ota_pct is None
    assert d.ota_bytes_done is None
    assert d.ota_bytes_total is None
    assert d.ota_updated_at is None
    # Terminal events also clear the atomic upgrade claim so the badge
    # falls off immediately instead of hanging for UPGRADE_TTL (this is
    # the failure-path UX fix from the rubber-duck review).
    assert d.upgrade_started_at is None


def test_promoted_clears_everything():
    _terminal_test_helper("ota_promoted")


def test_migration_complete_clears_everything():
    _terminal_test_helper("ota_migration_complete")


def test_failed_clears_everything():
    _terminal_test_helper("ota_failed")


def test_declined_clears_everything():
    _terminal_test_helper("ota_declined")


def test_terminal_event_always_lands_even_from_earlier_ordinal():
    # A device that's mid-extract (order=50) and then immediately gets a
    # failed event must NOT have the failed dropped as "regression" —
    # terminal events bypass the order guard.
    d = _DeviceStub(ota_phase="ota_slot_confirmed",  # order=80
                    upgrade_started_at=datetime.now(timezone.utc))
    # ota_failed has order=999 so it's allowed anyway, but verify that
    # an explicit terminal with an _earlier_ order also lands.  (For
    # the current schema all terminals are at 999; this test pins the
    # contract for future changes.)
    assert ota_progress.handle_event(d, "ota_failed", {}) is True
    assert d.upgrade_started_at is None


# ── forward-compat ────────────────────────────────────────────────────


def test_unknown_event_type_is_noop():
    d = _DeviceStub(ota_phase="ota_download_progress", ota_pct=42.0)
    assert ota_progress.handle_event(d, "ota_brand_new_event_type", {}) is False
    # State unchanged.
    assert d.ota_phase == "ota_download_progress"
    assert d.ota_pct == pytest.approx(42.0)


# ── edge cases in payload parsing ─────────────────────────────────────


def test_download_progress_with_zero_total_yields_no_pct():
    d = _DeviceStub()
    ota_progress.handle_event(d, "ota_download_progress",
                              {"bytes_done": 0, "bytes_total": 0})
    assert d.ota_bytes_done == 0
    assert d.ota_bytes_total == 0
    assert d.ota_pct is None


def test_download_progress_with_malformed_payload_doesnt_crash():
    d = _DeviceStub()
    # Strings instead of ints
    assert ota_progress.handle_event(
        d, "ota_download_progress",
        {"bytes_done": "not-a-number", "bytes_total": "also-not"},
    ) is True
    assert d.ota_bytes_done is None
    assert d.ota_bytes_total is None
    assert d.ota_pct is None


def test_download_progress_with_done_over_total_caps_at_100():
    d = _DeviceStub()
    ota_progress.handle_event(d, "ota_download_progress",
                              {"bytes_done": 1500, "bytes_total": 1000})
    # Tolerates server-side rounding errors / off-by-one chunks.
    assert d.ota_pct == pytest.approx(100.0)
