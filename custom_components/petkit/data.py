"""Custom types for Petkit Smart Devices integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pypetkitapi import Feeder, Litter, Pet, Purifier, WaterFountain

if TYPE_CHECKING:
    from pypetkitapi.client import PetKitClient

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.loader import Integration

    from .coordinator import (
        PetkitBluetoothUpdateCoordinator,
        PetkitDataUpdateCoordinator,
        PetkitMediaUpdateCoordinator,
    )
    from .iot_mqtt import PetkitIotMqttListener

type PetkitConfigEntry = ConfigEntry[PetkitData]

# Custom types for Petkit Smart Devices integration
type PetkitDevices = Feeder | Litter | WaterFountain | Purifier | Pet


@dataclass
class PetkitData:
    """Data for the Petkit integration."""

    client: PetKitClient
    coordinator: PetkitDataUpdateCoordinator
    coordinator_media: PetkitMediaUpdateCoordinator
    coordinator_bluetooth: PetkitBluetoothUpdateCoordinator
    integration: Integration
    mqtt_listener: PetkitIotMqttListener | None = None
