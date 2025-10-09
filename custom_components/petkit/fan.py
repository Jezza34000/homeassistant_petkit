"""Switch platform for Petkit Smart Devices integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pypetkitapi import K2, DeviceAction, DeviceCommand, Purifier

from homeassistant.components.fan import (
    FanEntity,
    FanEntityDescription,
    FanEntityFeature,
)

from .const import LOGGER, POWER_ONLINE_STATE, PURIFIER_MODE
from .entity import PetKitDescSensorBase, PetkitEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import PetkitDataUpdateCoordinator
    from .data import PetkitConfigEntry, PetkitDevices


@dataclass(frozen=True, kw_only=True)
class PetkitFanDesc(PetKitDescSensorBase, FanEntityDescription):
    """A class that describes sensor entities."""

    preset_modes: Callable[[], list[str]] | None = None
    turn_on: Callable[[Any, Any], Any] | None = None
    turn_off: Callable[[Any, Any], Any] | None = None
    set_mode: Callable[[Any, Any, Any], Any] | None = None
    current_mode: Callable[[Any], str] | None = None


FAN_MAPPING: dict[type[PetkitDevices], list[PetkitFanDesc]] = {
    Purifier: [
        PetkitFanDesc(
            key="Air Purifier Fan",
            translation_key="air_purifier_fan",
            preset_modes=lambda: list(PURIFIER_MODE.values()),
            current_mode=lambda device: PURIFIER_MODE.get(device.state.mode, 0),
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.CONTROL_DEVICE, {DeviceAction.POWER: 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.CONTROL_DEVICE, {DeviceAction.POWER: 0}
            ),
            set_mode=lambda api, device, opt_value: api.send_api_request(
                device.id,
                DeviceCommand.CONTROL_DEVICE,
                {
                    DeviceAction.MODE: next(
                        key
                        for key, value in PURIFIER_MODE.items()
                        if value == opt_value
                    )
                },
            ),
            only_for_types=[K2],
        ),
    ],
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary_sensors using config entry."""
    devices = entry.runtime_data.client.petkit_entities.values()
    entities = [
        PetkitFan(
            coordinator=entry.runtime_data.coordinator,
            entity_description=entity_description,
            device=device,
        )
        for device in devices
        for device_type, entity_descriptions in FAN_MAPPING.items()
        if isinstance(device, device_type)
        for entity_description in entity_descriptions
        if entity_description.is_supported(device)  # Check if the entity is supported
    ]
    LOGGER.debug(
        "FAN : Adding %s (on %s available)",
        len(entities),
        sum(len(descriptors) for descriptors in FAN_MAPPING.values()),
    )
    async_add_entities(entities)


class PetkitFan(PetkitEntity, FanEntity):
    """Petkit Smart Devices Switch class."""

    entity_description: PetkitFanDesc

    def __init__(
        self,
        coordinator: PetkitDataUpdateCoordinator,
        entity_description: PetkitFanDesc,
        device: PetkitDevices,
    ) -> None:
        """Initialize the switch class."""
        super().__init__(coordinator, device)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self.device = device

    @property
    def available(self) -> bool:
        """Return if this button is available or not"""
        device_data = self.coordinator.data.get(self.device.id)
        if hasattr(device_data.state, "pim"):
            return device_data.state.pim in POWER_ONLINE_STATE
        return True

    @property
    def is_on(self) -> bool:
        """Determine if the purifier is On."""

        device_data = self.coordinator.data.get(self.device.id)
        if hasattr(device_data.state, "power"):
            return device_data.state.power in POWER_ONLINE_STATE
        return True

    @property
    def preset_modes(self) -> list:
        """Return the available preset modes."""

        return self.entity_description.preset_modes()

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode."""
        device_data = self.coordinator.data.get(self.device.id)
        if device_data:
            return self.entity_description.current_mode(device_data)
        return None

    @property
    def supported_features(self) -> int:
        """Return supported features."""

        return (
            FanEntityFeature.PRESET_MODE
            | FanEntityFeature.TURN_ON
            | FanEntityFeature.TURN_OFF
        )

    async def async_turn_on(
        self,
        speed: str | None = None,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on the switch."""
        LOGGER.debug("Turn Fan ON")
        res = await self.entity_description.turn_on(
            self.coordinator.config_entry.runtime_data.client, self.device
        )
        await self._update_coordinator_data(res)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        LOGGER.debug("Turn Fan OFF")
        res = await self.entity_description.turn_off(
            self.coordinator.config_entry.runtime_data.client, self.device
        )
        await self._update_coordinator_data(res)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set a preset mode for the purifier."""
        LOGGER.debug(
            "Setting value for : %s with value : %s",
            self.entity_description.key,
            preset_mode,
        )
        await self.entity_description.set_mode(
            self.coordinator.config_entry.runtime_data.client, self.device, preset_mode
        )

    async def _update_coordinator_data(self, result: bool) -> None:
        """Update the coordinator data based on the result."""
        await asyncio.sleep(1)
        await self.coordinator.async_request_refresh()
