"""Switch platform for Petkit Smart Devices integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pypetkitapi import (
    D3,
    D4H,
    D4S,
    D4SH,
    DEVICES_FEEDER,
    DEVICES_LITTER_BOX,
    DEVICES_WATER_FOUNTAIN,
    LITTER_WITH_CAMERA,
    T4,
    T5,
    T7,
    DeviceAction,
    DeviceCommand,
    Feeder,
    FeederCommand,
    LBCommand,
    Litter,
    LitterCommand,
    Pet,
    Purifier,
    WaterFountain,
)
from pypetkitapi.command import FountainAction

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription

from .const import LOGGER, POWER_ONLINE_STATE
from .entity import PetKitDescSensorBase, PetkitEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import PetkitDataUpdateCoordinator
    from .data import PetkitConfigEntry, PetkitDevices


@dataclass(frozen=True, kw_only=True)
class PetKitButtonDesc(PetKitDescSensorBase, ButtonEntityDescription):
    """A class that describes sensor entities."""

    action: Callable[PetkitConfigEntry]
    is_available: Callable[[PetkitDevices], bool] | None = None


COMMON_ENTITIES = []

BUTTON_MAPPING: dict[type[PetkitDevices], list[PetKitButtonDesc]] = {
    Feeder: [
        *COMMON_ENTITIES,
        PetKitButtonDesc(
            key="Reset desiccant",
            translation_key="reset_desiccant",
            action=lambda api, device: api.send_api_request(
                device.id, FeederCommand.RESET_DESICCANT
            ),
            only_for_types=DEVICES_FEEDER,
        ),
        PetKitButtonDesc(
            key="Cancel manual feed",
            translation_key="cancel_manual_feed",
            action=lambda api, device: api.send_api_request(
                device.id, FeederCommand.CANCEL_MANUAL_FEED
            ),
            only_for_types=DEVICES_FEEDER,
        ),
        PetKitButtonDesc(
            key="Call pet",
            translation_key="call_pet",
            action=lambda api, device: api.send_api_request(
                device.id, FeederCommand.CALL_PET
            ),
            only_for_types=[D3],
        ),
        PetKitButtonDesc(
            key="Food replenished",
            translation_key="food_replenished",
            action=lambda api, device: api.send_api_request(
                device.id, FeederCommand.FOOD_REPLENISHED
            ),
            only_for_types=[D4S, D4H, D4SH],
        ),
    ],
    Litter: [
        *COMMON_ENTITIES,
        PetKitButtonDesc(
            key="Scoop",
            translation_key="start_scoop",
            action=lambda api, device: api.send_api_request(
                device.id,
                DeviceCommand.CONTROL_DEVICE,
                {DeviceAction.START: LBCommand.CLEANING},
            ),
            only_for_types=DEVICES_LITTER_BOX,
            is_available=lambda device: device.state.work_state is None,
        ),
        PetKitButtonDesc(
            key="Maintenance mode",
            translation_key="start_maintenance",
            action=lambda api, device: api.send_api_request(
                device.id,
                DeviceCommand.CONTROL_DEVICE,
                {DeviceAction.START: LBCommand.MAINTENANCE},
            ),
            only_for_types=[T4, T5],
            is_available=lambda device: device.state.work_state is None,
        ),
        PetKitButtonDesc(
            key="Exit maintenance mode",
            translation_key="exit_maintenance",
            action=lambda api, device: api.send_api_request(
                device.id,
                DeviceCommand.CONTROL_DEVICE,
                {DeviceAction.END: LBCommand.MAINTENANCE},
            ),
            only_for_types=[T4, T5],
            is_available=lambda device: device.state.work_state is not None
            and device.state.work_state.work_mode == 9,
        ),
        PetKitButtonDesc(
            key="Dump litter",
            translation_key="dump_litter",
            action=lambda api, device: api.send_api_request(
                device.id,
                DeviceCommand.CONTROL_DEVICE,
                {DeviceAction.START: LBCommand.DUMPING},
            ),
            only_for_types=DEVICES_LITTER_BOX,
            ignore_types=[T7],  # T7 does not support Dumping
            is_available=lambda device: device.state.work_state is None,
        ),
        PetKitButtonDesc(
            key="Pause",
            translation_key="action_pause",
            action=lambda api, device: api.send_api_request(
                device.id,
                DeviceCommand.CONTROL_DEVICE,
                {
                    DeviceAction.STOP: api.petkit_entities[
                        device.id
                    ].state.work_state.work_mode
                },
            ),
            only_for_types=DEVICES_LITTER_BOX,
            is_available=lambda device: device.state.work_state is not None,
        ),
        PetKitButtonDesc(
            key="Continue",
            translation_key="action_continue",
            action=lambda api, device: api.send_api_request(
                device.id,
                DeviceCommand.CONTROL_DEVICE,
                {
                    DeviceAction.CONTINUE: api.petkit_entities[
                        device.id
                    ].state.work_state.work_mode
                },
            ),
            only_for_types=DEVICES_LITTER_BOX,
            is_available=lambda device: device.state.work_state is not None,
        ),
        PetKitButtonDesc(
            key="Reset",
            translation_key="action_reset",
            action=lambda api, device: api.send_api_request(
                device.id,
                DeviceCommand.CONTROL_DEVICE,
                {
                    DeviceAction.END: api.petkit_entities[
                        device.id
                    ].state.work_state.work_mode
                },
            ),
            only_for_types=DEVICES_LITTER_BOX,
            is_available=lambda device: device.state.work_state is not None,
        ),
        PetKitButtonDesc(
            # For T3/T4 only
            key="Deodorize T3 T4",
            translation_key="deodorize",
            action=lambda api, device: api.send_api_request(
                device.id,
                DeviceCommand.CONTROL_DEVICE,
                {DeviceAction.START: LBCommand.ODOR_REMOVAL},
            ),
            only_for_types=[T4],
            value=lambda device: device.k3_device,
        ),
        PetKitButtonDesc(
            # For T5 / T7 only using the N60 deodorizer
            key="Deodorize T5 T7",
            translation_key="deodorize",
            action=lambda api, device: api.send_api_request(
                device.id,
                DeviceCommand.CONTROL_DEVICE,
                {DeviceAction.START: LBCommand.ODOR_REMOVAL},
            ),
            only_for_types=[T5, T7],
            force_add=[T5, T7],
            is_available=lambda device: device.state.refresh_state is None,
        ),
        PetKitButtonDesc(
            key="Reset N50 odor eliminator",
            translation_key="reset_n50_odor_eliminator",
            action=lambda api, device: api.send_api_request(
                device.id, LitterCommand.RESET_N50_DEODORIZER
            ),
            only_for_types=DEVICES_LITTER_BOX,
            ignore_types=[T7],
        ),
        PetKitButtonDesc(
            key="Reset N60 odor eliminator",
            translation_key="reset_n60_odor_eliminator",
            action=lambda api, device: api.send_api_request(
                device.id,
                DeviceCommand.CONTROL_DEVICE,
                {DeviceAction.START: LBCommand.RESET_N60_DEODOR},
            ),
            only_for_types=LITTER_WITH_CAMERA,
        ),
        PetKitButtonDesc(
            key="Level litter",
            translation_key="level_litter",
            action=lambda api, device: api.send_api_request(
                device.id,
                DeviceCommand.CONTROL_DEVICE,
                {DeviceAction.START: LBCommand.LEVELING},
            ),
            is_available=lambda device: device.state.work_state is None,
        ),
    ],
    WaterFountain: [
        *COMMON_ENTITIES,
        PetKitButtonDesc(
            key="Reset filter",
            translation_key="reset_filter",
            action=lambda api, device: api.bluetooth_manager.send_ble_command(
                device.id, FountainAction.RESET_FILTER
            ),
            only_for_types=DEVICES_WATER_FOUNTAIN,
        ),
    ],
    Purifier: [*COMMON_ENTITIES],
    Pet: [*COMMON_ENTITIES],
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary_sensors using config entry."""
    devices = entry.runtime_data.client.petkit_entities.values()
    entities = [
        PetkitButton(
            coordinator=entry.runtime_data.coordinator,
            entity_description=entity_description,
            device=device,
        )
        for device in devices
        for device_type, entity_descriptions in BUTTON_MAPPING.items()
        if isinstance(device, device_type)
        for entity_description in entity_descriptions
        if entity_description.is_supported(device)  # Check if the entity is supported
    ]
    LOGGER.debug(
        "BUTTON : Adding %s (on %s available)",
        len(entities),
        sum(len(descriptors) for descriptors in BUTTON_MAPPING.values()),
    )
    async_add_entities(entities)


class PetkitButton(PetkitEntity, ButtonEntity):
    """Petkit Smart Devices Button class."""

    entity_description: PetKitButtonDesc

    def __init__(
        self,
        coordinator: PetkitDataUpdateCoordinator,
        entity_description: PetKitButtonDesc,
        device: Feeder | Litter | WaterFountain,
    ) -> None:
        """Initialize the switch class."""
        super().__init__(coordinator, device)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self.device = device

    @property
    def available(self) -> bool:
        """Only make available if device is online."""

        device_data = self.coordinator.data.get(self.device.id)
        try:
            if device_data.state.pim not in POWER_ONLINE_STATE:
                return False
        except AttributeError:
            pass

        if self.entity_description.is_available:
            is_available = self.entity_description.is_available(device_data)
            LOGGER.debug(
                "Button %s availability result is: %s",
                self.entity_description.key,
                is_available,
            )
            return is_available

        return True

    async def async_press(self) -> None:
        """Handle the button press."""
        LOGGER.debug("Button pressed: %s", self.entity_description.key)
        self.coordinator.enable_smart_polling(12)
        await self.entity_description.action(
            self.coordinator.config_entry.runtime_data.client, self.device
        )
        await asyncio.sleep(1.5)
        await self.coordinator.async_request_refresh()
