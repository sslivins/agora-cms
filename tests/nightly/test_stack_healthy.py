"""Phase 1 sanity test: the full compose stack comes up cleanly."""

from __future__ import annotations

import httpx
import pytest


def test_cms_healthz(cms_base_url: str) -> None:
    r = httpx.get(f"{cms_base_url}/healthz", timeout=5.0)
    assert r.status_code == 200, r.text


def test_cms_login_page_renders(cms_base_url: str) -> None:
    r = httpx.get(f"{cms_base_url}/login", timeout=5.0)
    assert r.status_code == 200
    assert "html" in r.headers.get("content-type", "").lower()


def test_mailpit_ready(mailpit) -> None:
    assert mailpit.is_ready()
    # Fresh mailbox after the conftest `delete_all`.
    assert mailpit.list_messages() == []


def test_simulator_ready_with_devices(simulator) -> None:
    devices = simulator.wait_for_devices(expected_count=3, timeout=90.0)
    assert len(devices) == 3
    for d in devices:
        assert d["serial"].startswith("sim-")
        assert d["ws_open"] is True, (
            f"device {d['serial']} not connected to CMS: {d!r}"
        )


def test_fault_injection_roundtrip(simulator) -> None:
    """Confirms the control plane actually mutates state, not just reads it."""
    devices = simulator.wait_for_devices(expected_count=3, timeout=60.0)
    serial = devices[0]["serial"]

    simulator.apply_fault(serial, cpu_temp=87.5)
    after = simulator.get_device(serial)
    assert after["fault"]["cpu_temp"] == pytest.approx(87.5)

    simulator.clear_faults(serial)
    cleared = simulator.get_device(serial)
    assert cleared["fault"]["cpu_temp"] is None
