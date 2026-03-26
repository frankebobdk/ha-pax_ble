"""Runtime data types for Pax BLE integration."""

from __future__ import annotations

from dataclasses import dataclass, field

from .coordinator import BaseCoordinator


@dataclass
class PaxBleData:
    """Runtime data stored in config entry."""

    devices: dict[str, BaseCoordinator] = field(default_factory=dict)
