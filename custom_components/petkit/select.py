"""Switch platform for Petkit Smart Devices integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pypetkitapi import (
    D4H,
    D4SH,
    T7,
    DeviceCommand,
    Feeder,
    Litter,
    Pet,
    Purifier,
    WaterFountain,
)

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory

from .const import (
    CLEANING_INTERVAL_OPT,
    IA_DETECTION_SENSITIVITY_OPT,
    LITTER_TYPE_OPT,
    LOGGER,
    POWER_ONLINE_STATE,
    SURPLUS_FOOD_LEVEL_OPT,
)
from .entity import PetKitDescSensorBase, PetkitEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import PetkitDataUpdateCoordinator
    from .data import PetkitConfigEntry, PetkitDevices


@dataclass(frozen=True, kw_only=True)
class PetKitSelectDesc(PetKitDescSensorBase, SelectEntityDescription):
    """A class that describes sensor entities."""

    current_option: Callable[[PetkitDevices], str] | None = None
    options: Callable[[], list[str]] | None = None
    action: Callable[PetkitConfigEntry]


COMMON_ENTITIES = []

SELECT_MAPPING: dict[type[PetkitDevices], list[PetKitSelectDesc]] = {
    Feeder: [
        *COMMON_ENTITIES,
        PetKitSelectDesc(
            key="Surplus level",
            translation_key="surplus_level",
            current_option=lambda device: SURPLUS_FOOD_LEVEL_OPT[
                device.settings.surplus_standard
            ],
            options=lambda: list(SURPLUS_FOOD_LEVEL_OPT.values()),
            action=lambda api, device, opt_value: api.send_api_request(
                device.id,
                DeviceCommand.UPDATE_SETTING,
                {
                    "surplusStandard": next(
                        key
                        for key, value in SURPLUS_FOOD_LEVEL_OPT.items()
                        if value == opt_value
                    )
                },
            ),
            only_for_types=[D4H, D4SH],
        ),
        PetKitSelectDesc(
            key="Eat detection sensitivity",
            translation_key="eat_detection_sensitivity",
            current_option=lambda device: IA_DETECTION_SENSITIVITY_OPT[
                device.settings.eat_sensitivity
            ],
            options=lambda: list(IA_DETECTION_SENSITIVITY_OPT.values()),
            action=lambda api, device, opt_value: api.send_api_request(
                device.id,
                DeviceCommand.UPDATE_SETTING,
                {
                    "eatSensitivity": next(
                        key
                        for key, value in IA_DETECTION_SENSITIVITY_OPT.items()
                        if value == opt_value
                    )
                },
            ),
            entity_category=EntityCategory.CONFIG,
            only_for_types=[D4H, D4SH],
        ),
        PetKitSelectDesc(
            key="Pet detection sensitivity",
            translation_key="pet_detection_sensitivity",
            current_option=lambda device: IA_DETECTION_SENSITIVITY_OPT[
                device.settings.pet_sensitivity
            ],
            options=lambda: list(IA_DETECTION_SENSITIVITY_OPT.values()),
            action=lambda api, device, opt_value: api.send_api_request(
                device.id,
                DeviceCommand.UPDATE_SETTING,
                {
                    "petSensitivity": next(
                        key
                        for key, value in IA_DETECTION_SENSITIVITY_OPT.items()
                        if value == opt_value
                    )
                },
            ),
            entity_category=EntityCategory.CONFIG,
            only_for_types=[D4H, D4SH],
        ),
        PetKitSelectDesc(
            key="Move detection sensitivity",
            translation_key="move_detection_sensitivity",
            current_option=lambda device: IA_DETECTION_SENSITIVITY_OPT[
                device.settings.move_sensitivity
            ],
            options=lambda: list(IA_DETECTION_SENSITIVITY_OPT.values()),
            action=lambda api, device, opt_value: api.send_api_request(
                device.id,
                DeviceCommand.UPDATE_SETTING,
                {
                    "moveSensitivity": next(
                        key
                        for key, value in IA_DETECTION_SENSITIVITY_OPT.items()
                        if value == opt_value
                    )
                },
            ),
            entity_category=EntityCategory.CONFIG,
            only_for_types=[D4H, D4SH],
        ),
    ],
    Litter: [
        *COMMON_ENTITIES,
        PetKitSelectDesc(
            key="Litter type",
            translation_key="litter_type",
            current_option=lambda device: LITTER_TYPE_OPT[device.settings.sand_type],
            options=lambda: list(LITTER_TYPE_OPT.values()),
            action=lambda api, device, opt_value: api.send_api_request(
                device.id,
                DeviceCommand.UPDATE_SETTING,
                {
                    "sandType": next(
                        key
                        for key, value in LITTER_TYPE_OPT.items()
                        if value == opt_value
                    )
                },
            ),
            entity_category=EntityCategory.CONFIG,
            ignore_types=[T7],
        ),
        PetKitSelectDesc(
            key="Avoid repeat cleaning interval",
            translation_key="avoid_repeat_cleaning_interval",
            current_option=lambda device: CLEANING_INTERVAL_OPT[
                device.settings.auto_interval_min
            ],
            options=lambda: list(CLEANING_INTERVAL_OPT.values()),
            action=lambda api, device, opt_value: api.send_api_request(
                device.id,
                DeviceCommand.UPDATE_SETTING,
                {
                    "autoIntervalMin": next(
                        key
                        for key, value in CLEANING_INTERVAL_OPT.items()
                        if value == opt_value
                    )
                },
            ),
            entity_category=EntityCategory.CONFIG,
        ),
    ],
    WaterFountain: [*COMMON_ENTITIES],
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
        PetkitSelect(
            coordinator=entry.runtime_data.coordinator,
            entity_description=entity_description,
            device=device,
        )
        for device in devices
        for device_type, entity_descriptions in SELECT_MAPPING.items()
        if isinstance(device, device_type)
        for entity_description in entity_descriptions
        if entity_description.is_supported(device)  # Check if the entity is supported
    ]
    LOGGER.debug(
        "SELECT : Adding %s (on %s available)",
        len(entities),
        sum(len(descriptors) for descriptors in SELECT_MAPPING.values()),
    )
    async_add_entities(entities)


class PetkitSelect(PetkitEntity, SelectEntity):
    """Petkit Smart Devices Select class."""

    entity_description: PetKitSelectDesc

    def __init__(
        self,
        coordinator: PetkitDataUpdateCoordinator,
        entity_description: PetKitSelectDesc,
        device: Feeder | Litter | WaterFountain,
    ) -> None:
        """Initialize the switch class."""
        super().__init__(coordinator, device)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self.device = device

    @property
    def current_option(self) -> str | None:
        """Return the current surplus food level option."""
        device_data = self.coordinator.data.get(self.device.id)
        if device_data:
            return self.entity_description.current_option(device_data)
        return None

    @property
    def options(self) -> list[str]:
        """Return list of all available manual feed amounts."""
        return self.entity_description.options()

    @property
    def available(self) -> bool:
        """Return if this button is available or not"""
        device_data = self.coordinator.data.get(self.device.id)
        if hasattr(device_data.state, "pim"):
            return device_data.state.pim in POWER_ONLINE_STATE
        return True

    async def async_select_option(self, value: str) -> None:
        """Set manual feeding amount."""
        LOGGER.debug(
            "Setting value for : %s with value : %s", self.entity_description.key, value
        )
        await self.entity_description.action(
            self.coordinator.config_entry.runtime_data.client, self.device, value
        )
