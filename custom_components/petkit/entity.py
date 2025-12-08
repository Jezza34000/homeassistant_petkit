"""Petkit Smart Devices Entity class."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pypetkitapi import Feeder, Litter, Pet, Purifier, WaterFountain

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOGGER, PETKIT_DEVICES_MAPPING
from .coordinator import (
    PetkitBluetoothUpdateCoordinator,
    PetkitDataUpdateCoordinator,
    PetkitMediaUpdateCoordinator,
)
from .data import PetkitDevices

_DevicesT = TypeVar("_DevicesT", bound=Feeder | Litter | WaterFountain | Purifier | Pet)


@dataclass(frozen=True, kw_only=True)
class PetKitDescSensorBase(EntityDescription):
    """A class that describes sensor entities."""

    value: Callable[[_DevicesT], Any] | None = None
    ignore_types: list[str] | None = None  # List of device types to ignore
    only_for_types: list[str] | None = None  # List of device types to support
    force_add: list[str] | None = None
    entity_picture: Callable[[PetkitDevices], str | None] | None = None

    def is_supported(self, device: _DevicesT) -> bool:
        """Check if the entity is supported by trying to execute the value lambda."""

        if not isinstance(device, Feeder | Litter | WaterFountain | Purifier | Pet):
            LOGGER.error(
                f"Device instance is not of expected type: {type(device)} can't check support"
            )
            return False

        device_type = getattr(device.device_nfo, "device_type", None)
        if not device_type:
            LOGGER.error(f"Entities {device.name} has no type, can't check support")
            return False
        device_type = device_type.lower()

        if self._is_force_added(device_type):
            return True

        if self._is_ignored(device_type):
            return False

        if self._is_not_in_supported_types(device_type):
            return False

        return self._check_value_support(device)

    def _is_force_added(self, device_type: str) -> bool:
        """Check if the device is in the force_add list."""
        if self.force_add and device_type in self.force_add:
            LOGGER.debug(f"{device_type} force add for '{self.key}'")
            return True
        return False

    def _is_ignored(self, device_type: str) -> bool:
        """Check if the device is in the ignore_types list."""
        if self.ignore_types and device_type in self.ignore_types:
            LOGGER.debug(f"{device_type} force ignore for '{self.key}'")
            return True
        return False

    def _is_not_in_supported_types(self, device_type: str) -> bool:
        """Check if the device is not in the only_for_types list."""
        if self.only_for_types and device_type not in self.only_for_types:
            LOGGER.debug(f"{device_type} is NOT COMPATIBLE with '{self.key}'")
            return True
        return False

    def _check_value_support(self, device: _DevicesT) -> bool:
        """Check if the device supports the value lambda."""
        if self.value is not None:
            try:
                result = self.value(device)
                if result is None:
                    LOGGER.debug(
                        f"{device.device_nfo.device_type} DOES NOT support '{self.key}' (value is None)"
                    )
                    return False
                LOGGER.debug(f"{device.device_nfo.device_type} supports '{self.key}'")
            except AttributeError:
                LOGGER.debug(
                    f"{device.device_nfo.device_type} DOES NOT support '{self.key}'"
                )
                return False
        return True


class PetkitEntity(
    CoordinatorEntity[
        PetkitDataUpdateCoordinator
        | PetkitMediaUpdateCoordinator
        | PetkitBluetoothUpdateCoordinator
    ],
    Generic[_DevicesT],
):
    """Petkit Entity class."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: (
            PetkitDataUpdateCoordinator
            | PetkitMediaUpdateCoordinator
            | PetkitBluetoothUpdateCoordinator
        ),
        device: _DevicesT,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.device = device
        self._attr_unique_id = coordinator.config_entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={
                (
                    coordinator.config_entry.domain,
                    coordinator.config_entry.entry_id,
                ),
            },
        )

    @property
    def unique_id(self) -> str:
        """Return a unique ID for the binary_sensor."""
        return f"{self.device.device_nfo.device_type}_{self.device.sn}_{self.entity_description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device information"""

        if self.device.device_nfo.device_type:
            device_type = self.device.device_nfo.device_type
            device_model = PETKIT_DEVICES_MAPPING.get(
                str(self.device.device_nfo.type_code) + str(device_type.lower()),
                "Unknown Device",
            )
        else:
            device_type = "Unknown"
            device_model = "Unknown Device"

        device_info = DeviceInfo(
            identifiers={(DOMAIN, self.device.sn)},
            manufacturer="Petkit",
            model=device_model,
            model_id=device_type.upper(),
            name=self.device.name,
        )

        if not isinstance(self.device, Pet):
            if self.device.mac is not None:
                device_info["connections"] = {(CONNECTION_NETWORK_MAC, self.device.mac)}

            if self.device.firmware is not None:
                device_info["sw_version"] = str(self.device.firmware)

            if self.device.hardware is not None:
                device_info["hw_version"] = str(self.device.hardware)

            if self.device.sn is not None:
                device_info["serial_number"] = str(self.device.sn)

        return device_info
