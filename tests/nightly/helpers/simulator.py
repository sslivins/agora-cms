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
