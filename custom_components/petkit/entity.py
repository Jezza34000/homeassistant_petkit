"""Petkit Smart Devices Entity class."""

from __future__ import annotations

from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pypetkitapi import Feeder, Litter, Pet, Purifier, WaterFountain

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOGGER
from .coordinator import (
    PetkitBluetoothUpdateCoordinator,
    PetkitDataUpdateCoordinator,
    PetkitMediaUpdateCoordinator,
)
from .data import PetkitDevices

_DevicesT = TypeVar("_DevicesT", bound=Feeder | Litter | WaterFountain | Purifier | Pet)


def _build_device_info(
    device: Feeder | Litter | WaterFountain | Purifier | Pet,
) -> DeviceInfo:
    """Build a DeviceInfo dict from a Petkit device."""

    device_info = DeviceInfo(
        identifiers={(DOMAIN, device.sn)},
        manufacturer="Petkit",
        model=device.device_nfo.modele_name,
        model_id=device.device_nfo.device_type.upper(),
        name=device.name,
    )

    if not isinstance(device, Pet):
        if device.mac is not None:
            device_info["connections"] = {(CONNECTION_NETWORK_MAC, device.mac)}

        if device.firmware is not None:
            device_info["sw_version"] = str(device.firmware)

        if device.hardware is not None:
            device_info["hw_version"] = str(device.hardware)

        if device.sn is not None:
            device_info["serial_number"] = str(device.sn)

    return device_info


@dataclass(frozen=True, kw_only=True)
class PetKitDescSensorBase(EntityDescription):
    """A class that describes sensor entities."""

    value: Callable[[_DevicesT], Any] | None = None
    attributes: Callable[[_DevicesT], dict[str, Any] | None] | None = None
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
            LOGGER.error("Entities %s has no type, can't check support", device.name)
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
            LOGGER.debug("%s force add for '%s'", device_type, self.key)
            return True
        return False

    def _is_ignored(self, device_type: str) -> bool:
        """Check if the device is in the ignore_types list."""
        if self.ignore_types and device_type in self.ignore_types:
            LOGGER.debug("%s force ignore for '%s'", device_type, self.key)
            return True
        return False

    def _is_not_in_supported_types(self, device_type: str) -> bool:
        """Check if the device is not in the only_for_types list."""
        if self.only_for_types and device_type not in self.only_for_types:
            LOGGER.debug("%s is NOT COMPATIBLE with '%s'", device_type, self.key)
            return True
        return False

    def _check_value_support(self, device: _DevicesT) -> bool:
        """Check if the device supports the value lambda."""
        if self.value is not None:
            try:
                result = self.value(device)
                if result is None:
                    LOGGER.debug(
                        "%s DOES NOT support '%s' (value is None)",
                        device.device_nfo.device_type,
                        self.key,
                    )
                    return False
                LOGGER.debug(
                    "%s supports '%s'", device.device_nfo.device_type, self.key
                )
            except AttributeError:
                LOGGER.debug(
                    "%s DOES NOT support '%s'", device.device_nfo.device_type, self.key
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
        """Return the device information."""
        return _build_device_info(self.device)


class PetkitCameraBaseEntity(
    Camera,
    CoordinatorEntity[PetkitDataUpdateCoordinator],
    ABC,
):
    """Base class for PetKit camera entities."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_is_streaming = True
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: PetkitDataUpdateCoordinator,
        device: Feeder | Litter | WaterFountain,
        key: str,
    ) -> None:
        """Initialize the camera entity."""
        Camera.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        self.coordinator = coordinator
        self.device = device
        self._attr_unique_id = (
            f"{self.device.device_nfo.device_type}_{self.device.sn}_{key}"
        )

    @property
    def unique_id(self) -> str:
        """Return the entity unique ID."""
        return self._attr_unique_id

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device information."""
        return _build_device_info(self.device)
