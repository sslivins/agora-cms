"""Helpers for the nightly E2E test suite."""

from tests.nightly.helpers.bootstrap_register import (
    NIGHTLY_FLEET_ID,
    NIGHTLY_FLEET_SECRET_B64,
    RegisteredDevice,
    register_pending_device,
)
from tests.nightly.helpers.mailpit import MailpitClient
from tests.nightly.helpers.simulator import SimulatorClient

__all__ = [
    "MailpitClient",
    "NIGHTLY_FLEET_ID",
    "NIGHTLY_FLEET_SECRET_B64",
    "RegisteredDevice",
    "SimulatorClient",
    "register_pending_device",
]
