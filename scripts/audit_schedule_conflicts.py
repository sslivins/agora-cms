#!/usr/bin/env python3
"""Read-only audit: find schedule pairs that can collide on a shared device.

Two schedules conflict when they target the same group, run at the same
priority, and their time windows overlap on a shared day -- unless they are
narrowed to different tags whose device sets are disjoint. The write paths
(``POST/PATCH /api/schedules`` and ``PUT /api/devices/{id}/tags``) reject new
conflicts, but rows that predate those checks can still be sitting in the
database. This script reports them; it never writes.

Usage::

    python scripts/audit_schedule_conflicts.py \
        --url https://cms.example.com --username admin --password ...

Exits 0 when clean, 1 when at least one conflicting pair is found, so it can
be dropped into a cron/CI check.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from itertools import combinations

import requests


def _parse_time(value: str) -> tuple[int, int]:
    hh, mm = value.split(":")[:2]
    return int(hh), int(mm)


def _minutes(value: str) -> int:
    hh, mm = _parse_time(value)
    return hh * 60 + mm


def _windows_overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    """Overlap test that understands overnight windows (end <= start)."""
    def spans(start: str, end: str) -> list[tuple[int, int]]:
        s, e = _minutes(start), _minutes(end)
        # An overnight window is modelled as two same-day spans so the
        # comparison below stays a simple interval intersection.
        return [(s, 24 * 60), (0, e)] if e <= s else [(s, e)]

    for s1, e1 in spans(a_start, a_end):
        for s2, e2 in spans(b_start, b_end):
            if s1 < e2 and s2 < e1:
                return True
    return False


def _days_overlap(a: list[int] | None, b: list[int] | None) -> bool:
    # Null/empty means "every day".
    if not a or not b:
        return True
    return bool(set(a) & set(b))


def _dates_overlap(a: dict, b: dict) -> bool:
    a_start, a_end = a.get("start_date"), a.get("end_date")
    b_start, b_end = b.get("start_date"), b.get("end_date")
    if a_end and b_start and a_end < b_start:
        return False
    if b_end and a_start and b_end < a_start:
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True, help="CMS base URL")
    ap.add_argument("--username", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    base = args.url.rstrip("/")
    session = requests.Session()
    resp = session.post(
        f"{base}/login",
        data={"username": args.username, "password": args.password},
        timeout=args.timeout,
        allow_redirects=False,
    )
    if resp.status_code not in (200, 302, 303):
        resp.raise_for_status()
        raise SystemExit(f"login failed: HTTP {resp.status_code}")

    schedules = session.get(f"{base}/api/schedules", timeout=args.timeout).json()
    devices = session.get(f"{base}/api/devices", timeout=args.timeout).json()

    # tag id -> set of device ids currently carrying it. DeviceOut does not
    # embed tags, so this needs one call per device; an audit runs rarely
    # enough that the extra round trips are acceptable.
    tag_devices: dict[str, set[str]] = defaultdict(set)
    group_devices: dict[str, set[str]] = defaultdict(set)
    for d in devices:
        gid = d.get("group_id")
        if gid:
            group_devices[gid].add(d["id"])
        r = session.get(f"{base}/api/devices/{d['id']}/tags", timeout=args.timeout)
        if r.status_code != 200:
            continue
        for t in r.json().get("tags") or []:
            tag_devices[str(t["id"])].add(d["id"])

    def target_devices(s: dict) -> set[str]:
        gid = s.get("group_id")
        if not gid:
            return set()
        members = group_devices.get(gid, set())
        tag_id = s.get("tag_id")
        if not tag_id:
            return set(members)
        return members & tag_devices.get(str(tag_id), set())

    by_group: dict[str, list[dict]] = defaultdict(list)
    for s in schedules:
        if not s.get("enabled"):
            continue
        gid = s.get("group_id")
        if gid:
            by_group[gid].append(s)

    findings = []
    for gid, group_schedules in by_group.items():
        for a, b in combinations(group_schedules, 2):
            if a.get("priority") != b.get("priority"):
                continue
            if not _days_overlap(a.get("days_of_week"), b.get("days_of_week")):
                continue
            if not _dates_overlap(a, b):
                continue
            if not _windows_overlap(
                a["start_time"], a["end_time"], b["start_time"], b["end_time"]
            ):
                continue
            shared = target_devices(a) & target_devices(b)
            if not shared:
                continue
            findings.append((a, b, sorted(shared)))

    if not findings:
        print(f"Clean: no conflicting schedule pairs across {len(schedules)} schedules.")
        return 0

    print(f"Found {len(findings)} conflicting schedule pair(s):\n")
    for a, b, shared in findings:
        print(f"  group: {a.get('group_name') or a.get('group_id')}  priority: {a.get('priority')}")
        print(f"    A: {a['name']}  {a['start_time']}-{a['end_time']}  tag={a.get('tag_name') or '(all)'}")
        print(f"    B: {b['name']}  {b['start_time']}-{b['end_time']}  tag={b.get('tag_name') or '(all)'}")
        print(f"    shared devices ({len(shared)}): {', '.join(shared)}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
