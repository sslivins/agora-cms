"""Unit tests for ``humanize_seconds()`` in alert_service.

Used in the offline alert notification body to render durations as
"5 minutes" instead of "300 seconds".  See PR for issue/polish work
following PR #440.
"""

from __future__ import annotations

import pytest

from cms.services.alert_service import humanize_seconds


@pytest.mark.parametrize("n,expected", [
    (0, "0 seconds"),
    (1, "1 second"),
    (2, "2 seconds"),
    (59, "59 seconds"),
    (60, "1 minute"),
    (61, "1 minute 1 second"),
    (90, "1 minute 30 seconds"),
    (119, "1 minute 59 seconds"),
    (120, "2 minutes"),
    (300, "5 minutes"),
    (3599, "59 minutes 59 seconds"),
    (3600, "1 hour"),
    (3601, "1 hour 1 second"),
    (3660, "1 hour 1 minute"),
    (3661, "1 hour 1 minute 1 second"),
    (7200, "2 hours"),
    (7320, "2 hours 2 minutes"),
])
def test_humanize_seconds(n, expected):
    assert humanize_seconds(n) == expected


def test_humanize_seconds_negative_clamped_to_zero():
    """Defensive: never raise on a bogus negative input."""
    assert humanize_seconds(-1) == "0 seconds"
    assert humanize_seconds(-3600) == "0 seconds"
