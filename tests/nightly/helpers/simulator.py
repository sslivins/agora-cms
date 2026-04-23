"""Client for the agora-device-simulator fault-injection control plane.

The simulator exposes an HTTP API (default: http://<host>:9090) with
routes documented in agora-device-simulator/README.md:

    GET    /devices
    GET    /devices/{serial}
    POST   /devices/{serial}/fault       {"cpu_temp":88,"codecs":["h264"],...}
    DELETE /devices/{serial}/fault
    POST   /devices/{serial}/offline     {"duration_sec": 30}
    POST   /fleet/fault
    POST   /fleet/offline
"""

from __future__ import annotations

import time
from typing import Any

import httpx


class SimulatorClient:
    def __init__(self, base_url: str, *, timeout: float = 5.0):
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self._base, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SimulatorClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def list_devices(self) -> list[dict]:
        r = self._client.get("/devices")
        r.raise_for_status()
        return r.json().get("devices", [])

    def get_device(self, serial: str) -> dict:
        r = self._client.get(f"/devices/{serial}")
        r.raise_for_status()
        return r.json()

    def serials(self) -> list[str]:
        return [d["serial"] for d in self.list_devices()]

    def apply_fault(self, serial: str, **fault: Any) -> dict:
        r = self._client.post(f"/devices/{serial}/fault", json=fault)
        r.raise_for_status()
        return r.json()

    def clear_faults(self, serial: str) -> dict:
        r = self._client.delete(f"/devices/{serial}/fault")
        r.raise_for_status()
        return r.json()

    def force_offline(self, serial: str, duration_sec: float) -> dict:
        r = self._client.post(
            f"/devices/{serial}/offline", json={"duration_sec": duration_sec}
        )
        r.raise_for_status()
        return r.json()

    def fleet_fault(self, **fault: Any) -> dict:
        r = self._client.post("/fleet/fault", json=fault)
        r.raise_for_status()
        return r.json()

    def fleet_offline(self, duration_sec: float) -> dict:
        r = self._client.post(
            "/fleet/offline", json={"duration_sec": duration_sec}
        )
        r.raise_for_status()
        return r.json()

    # --- recording / now-playing inspection (PR #2 on agora-device-simulator) --

    def get_recording(self, serial: str) -> dict:
        """Fetch the device's inbound-command ring buffer + counters."""
        r = self._client.get(f"/devices/{serial}/recording")
        r.raise_for_status()
        return r.json()

    def reset_recording(self, serial: str) -> dict:
        """Clear recorded commands + counters for per-test isolation."""
        r = self._client.delete(f"/devices/{serial}/recording")
        r.raise_for_status()
        return r.json()

    def get_now_playing(self, serial: str) -> dict:
        r = self._client.get(f"/devices/{serial}/now-playing")
        r.raise_for_status()
        return r.json().get("now_playing")

    def set_logs(self, serial: str, content: dict[str, int | str]) -> dict:
        """Configure synthetic journalctl output for ``request_logs`` smoke tests.

        Each value is either an ``int`` (synthesize that many bytes of
        ASCII filler — useful for sizing payloads against the firmware
        ``LOGS_JSON_MAX_BYTES`` threshold) or a ``str`` (literal text).

        Requires sim image with ``POST /devices/{serial}/logs`` endpoint
        (agora-device-simulator PR #6 / commit cec9b97).
        """
        r = self._client.post(f"/devices/{serial}/logs", json=content)
        r.raise_for_status()
        return r.json()

    def clear_logs(self, serial: str) -> dict:
        r = self._client.delete(f"/devices/{serial}/logs")
        r.raise_for_status()
        return r.json()

    def wait_for_command(
        self, serial: str, command_type: str,
        *, count: int = 1, timeout: float = 10.0,
        poll_interval: float = 0.2,
    ) -> list[dict]:
        """Poll the recording until we see `count` commands of `command_type`.

        Returns the matching commands (most-recent last). Raises AssertionError
        on timeout with the last snapshot for diagnosability.
        """
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.get_recording(serial)
            matches = [c for c in last.get("commands", []) if c.get("type") == command_type]
            if len(matches) >= count:
                return matches
            time.sleep(poll_interval)
        raise AssertionError(
            f"device {serial} did not receive {count}x '{command_type}' "
            f"within {timeout}s. Recording: {last!r}"
        )

    def is_ready(self) -> bool:
        try:
            return self._client.get("/devices").status_code == 200
        except httpx.HTTPError:
            return False

    def wait_for_devices(
        self, expected_count: int, *, timeout: float = 60.0,
        poll_interval: float = 0.5, require_ws_open: bool = True,
    ) -> list[dict]:
        """Block until N devices are registered (and optionally connected)."""
        deadline = time.monotonic() + timeout
        last: list[dict] = []
        while time.monotonic() < deadline:
            try:
                last = self.list_devices()
            except httpx.HTTPError:
                last = []
            if len(last) >= expected_count and (
                not require_ws_open
                or sum(1 for d in last if d.get("ws_open")) >= expected_count
            ):
                return last
            time.sleep(poll_interval)
        raise TimeoutError(
            f"Expected {expected_count} devices (ws_open={require_ws_open}) "
            f"after {timeout}s, last snapshot: {last!r}"
        )
