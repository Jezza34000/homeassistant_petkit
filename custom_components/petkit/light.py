"""Light platform for Petkit Smart Devices integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pypetkitapi import LITTER_WITH_CAMERA, T4, DeviceAction, DeviceCommand, LBCommand

from homeassistant.components.light import (
    ColorMode,
    LightEntity,
    LightEntityDescription,
)

from .const import LOGGER, MIN_SCAN_INTERVAL, POWER_ONLINE_STATE
from .entity import PetKitDescSensorBase, PetkitEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import PetkitConfigEntry


@dataclass(frozen=True, kw_only=True)
class PetKitLightDesc(PetKitDescSensorBase, LightEntityDescription):
    """A class for describing Petkit light entities."""

    turn_on: Callable[[Any, Any], Any] | None = None
    turn_off: Callable[[Any, Any], Any] | None = None


def get_k3_light_value(device):
    """Get the light value for K3 devices."""
    if device.k3_device is None:
        return None
    if device.state.light_state is not None:
        return device.state.light_state
    return 0


LIGHT_ENTITIES = [
    PetKitLightDesc(
        # For K3 or K3 (binded to T4)
        key="Light K3",
        translation_key="light",
        value=get_k3_light_value,
        turn_on=lambda api, device: api.send_api_request(
            device.id,
            DeviceCommand.CONTROL_DEVICE,
            {DeviceAction.START: LBCommand.LIGHT},
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id,
            DeviceCommand.CONTROL_DEVICE,
            {DeviceAction.END: LBCommand.LIGHT},
        ),
        only_for_types=[T4],
    ),
    PetKitLightDesc(
        # For T5 / T6
        key="Light camera",
        translation_key="light",
        value=lambda device: (
            device.state.light_state.work_process
            if device.state.light_state is not None
            else 0
        ),
        turn_on=lambda api, device: api.send_api_request(
            device.id,
            DeviceCommand.CONTROL_DEVICE,
            {DeviceAction.START: LBCommand.LIGHT},
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id,
            DeviceCommand.CONTROL_DEVICE,
            {DeviceAction.END: LBCommand.LIGHT},
        ),
        only_for_types=LITTER_WITH_CAMERA,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up light entities using config entry."""
    devices = entry.runtime_data.client.petkit_entities.values()
    entities = [
        PetkitLight(
            coordinator=entry.runtime_data.coordinator,
            entity_description=entity_description,
            device=device,
        )
        for device in devices
        for entity_description in LIGHT_ENTITIES
        if entity_description.is_supported(device)
    ]
    LOGGER.debug(
        "LIGHT: Adding %s light entities.",
        len(entities),
    )
    async_add_entities(entities)


class PetkitLight(PetkitEntity, LightEntity):
    """Petkit Smart Devices Light class."""

    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF

    entity_description: PetKitLightDesc

    def __init__(
        self,
        coordinator,
        entity_description: PetKitLightDesc,
        device: Any,
    ) -> None:
        """Initialize the light class."""
        super().__init__(coordinator, device)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self.device = device

    @property
    def available(self) -> bool:
        """Return if this light is available or not."""
        device_data = self.coordinator.data.get(self.device.id)
        if hasattr(device_data.state, "pim"):
            return device_data.state.pim in POWER_ONLINE_STATE
        return True

    @property
    def is_on(self) -> bool | None:
        """Return true if the light is on."""
        updated_device = self.coordinator.data.get(self.device.id)
        if updated_device and self.entity_description.value:
            return bool(self.entity_description.value(updated_device))
        return None

    async def async_turn_on(self, **_: Any) -> None:
        """Turn on the light."""
        LOGGER.debug("Turn ON Light")
        res = await self.entity_description.turn_on(
            self.coordinator.config_entry.runtime_data.client, self.device
        )
        await self._update_coordinator_data(res)

    async def async_turn_off(self, **_: Any) -> None:
        """Turn off the light."""
        LOGGER.debug("Turn OFF Light")
        res = await self.entity_description.turn_off(
            self.coordinator.config_entry.runtime_data.client, self.device
        )
        await self._update_coordinator_data(res)

    async def _update_coordinator_data(self, result: bool) -> None:
        """Update the coordinator data based on the result."""
        self.coordinator.update_interval = timedelta(seconds=MIN_SCAN_INTERVAL)
        self.coordinator.fast_poll_tic = 3
        await asyncio.sleep(1)
        await self.coordinator.async_request_refresh()
