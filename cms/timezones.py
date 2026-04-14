"""Curated IANA timezone list for the CMS and Raspberry Pi devices.

Provides a hand-picked set of ~35 major timezones that cover every practical
UTC offset worldwide.  All names are canonical IANA ``Region/City`` identifiers
guaranteed to work with both Python ``zoneinfo`` and ``timedatectl`` on Pi
devices.

Using a curated list keeps the timezone picker compact and easy to navigate.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone as _tz

from zoneinfo import ZoneInfo

# Hand-curated list of major timezones covering all practical UTC offsets.
# Sorted roughly west-to-east by UTC offset.
CURATED_TIMEZONES: frozenset[str] = frozenset({
    "Pacific/Midway",        # UTC-11:00
    "Pacific/Honolulu",      # UTC-10:00
    "America/Anchorage",     # UTC-09:00
    "America/Los_Angeles",   # UTC-08:00  (Pacific)
    "America/Denver",        # UTC-07:00  (Mountain)
    "America/Phoenix",       # UTC-07:00  (Arizona, no DST)
    "America/Chicago",       # UTC-06:00  (Central)
    "America/New_York",      # UTC-05:00  (Eastern)
    "America/Halifax",       # UTC-04:00  (Atlantic)
    "America/St_Johns",      # UTC-03:30  (Newfoundland)
    "America/Sao_Paulo",     # UTC-03:00
    "America/Argentina/Buenos_Aires",  # UTC-03:00
    "Atlantic/South_Georgia",  # UTC-02:00
    "Atlantic/Azores",       # UTC-01:00
    "UTC",                   # UTC+00:00
    "Europe/London",         # UTC+00:00  (GMT/BST)
    "Europe/Paris",          # UTC+01:00  (CET)
    "Europe/Berlin",         # UTC+01:00  (CET)
    "Africa/Cairo",          # UTC+02:00  (EET)
    "Europe/Helsinki",       # UTC+02:00  (EET)
    "Asia/Jerusalem",        # UTC+02:00  (IST)
    "Europe/Moscow",         # UTC+03:00  (MSK)
    "Asia/Riyadh",           # UTC+03:00
    "Asia/Tehran",           # UTC+03:30
    "Asia/Dubai",            # UTC+04:00  (GST)
    "Asia/Karachi",          # UTC+05:00  (PKT)
    "Asia/Kolkata",          # UTC+05:30  (IST)
    "Asia/Kathmandu",        # UTC+05:45
    "Asia/Dhaka",            # UTC+06:00  (BST)
    "Asia/Bangkok",          # UTC+07:00  (ICT)
    "Asia/Shanghai",         # UTC+08:00  (CST)
    "Asia/Singapore",        # UTC+08:00  (SGT)
    "Asia/Tokyo",            # UTC+09:00  (JST)
    "Australia/Adelaide",    # UTC+09:30  (ACST)
    "Australia/Sydney",      # UTC+10:00  (AEST)
    "Pacific/Noumea",        # UTC+11:00
    "Pacific/Auckland",      # UTC+12:00  (NZST)
    "Pacific/Tongatapu",     # UTC+13:00
})


def canonical_timezones() -> frozenset[str]:
    """Return the curated set of timezone names safe for device use."""
    return CURATED_TIMEZONES


def build_tz_options(*, current_timezone: str = "UTC") -> list[dict[str, str]]:
    """Build a sorted list of ``{value, label}`` dicts for a timezone picker.

    Each label shows the timezone with underscores replaced by spaces and the
    current UTC offset, e.g. ``"America / New York (UTC-04:00)"``.
    Sorted by UTC offset then by name.
    """
    now_utc = datetime.now(_tz.utc)
    options: list[tuple[int, str, dict[str, str]]] = []
    for tz_name in CURATED_TIMEZONES:
        try:
            offset = now_utc.astimezone(ZoneInfo(tz_name)).utcoffset()
            total_sec = int(offset.total_seconds())  # type: ignore[union-attr]
        except Exception:
            total_sec = 0
        sign = "+" if total_sec >= 0 else "-"
        h, m = divmod(abs(total_sec) // 60, 60)
        label = f"{tz_name.replace('_', ' ')} (UTC{sign}{h:02d}:{m:02d})"
        options.append((total_sec, tz_name, {"value": tz_name, "label": label}))
    options.sort(key=lambda x: (x[0], x[1]))
    return [o[2] for o in options]
