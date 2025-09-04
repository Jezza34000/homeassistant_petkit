"""Switch platform for Petkit Smart Devices integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pypetkitapi import (
    D3,
    D4H,
    D4S,
    D4SH,
    FEEDER,
    T5,
    T6,
    DeviceCommand,
    Feeder,
    FeederCommand,
    Litter,
    Pet,
    PetCommand,
    Purifier,
    WaterFountain,
)
from pypetkitapi.const import PET

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfMass, UnitOfTime

from .const import LOGGER, POWER_ONLINE_STATE
from .entity import PetKitDescSensorBase, PetkitEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import PetkitDataUpdateCoordinator
    from .data import PetkitConfigEntry, PetkitDevices


@dataclass(frozen=True, kw_only=True)
class PetKitNumberDesc(PetKitDescSensorBase, NumberEntityDescription):
    """A class that describes number entities."""

    entity_picture: Callable[[PetkitDevices], str | None] | None = None
    native_value: Callable[[PetkitDevices], None] | None = None
    action: Callable[[PetkitConfigEntry, PetkitDevices, str], Any] | None


COMMON_ENTITIES = [
    PetKitNumberDesc(
        key="Volume",
        translation_key="volume",
        entity_category=EntityCategory.CONFIG,
        native_min_value=1,
        native_max_value=9,
        native_step=1,
        mode=NumberMode.SLIDER,
        native_value=lambda device: device.settings.volume,
        action=lambda api, device, value: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"volume": int(value)}
        ),
        only_for_types=[T5, T6, D3, D4H, D4SH],
    ),
]


NUMBER_MAPPING: dict[type[PetkitDevices], list[PetKitNumberDesc]] = {
    Feeder: [
        *COMMON_ENTITIES,
        PetKitNumberDesc(
            key="Surplus",
            translation_key="surplus",
            native_min_value=20,
            native_max_value=100,
            native_step=10,
            mode=NumberMode.SLIDER,
            native_value=lambda device: device.settings.surplus,
            action=lambda api, device, value: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"surplus": int(value)}
            ),
            only_for_types=[D3],
        ),
        PetKitNumberDesc(
            key="Min Eating Duration",
            translation_key="min_eating_duration",
            entity_category=EntityCategory.CONFIG,
            native_min_value=3,
            native_max_value=60,
            native_step=1,
            native_unit_of_measurement=UnitOfTime.SECONDS,
            mode=NumberMode.SLIDER,
            native_value=lambda device: device.settings.shortest,
            action=lambda api, device, value: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"shortest": int(value)}
            ),
            only_for_types=[D4S],
        ),
        PetKitNumberDesc(
            key="Manual Feed",
            translation_key="manual_feed",
            entity_category=EntityCategory.CONFIG,
            native_min_value=0,
            native_max_value=400,
            native_step=20,
            device_class=NumberDeviceClass.WEIGHT,
            mode=NumberMode.SLIDER,
            native_value=lambda device: 0,
            action=lambda api, device, value: api.send_api_request(
                device.id, FeederCommand.MANUAL_FEED, {"amount": int(value)}
            ),
            only_for_types=[FEEDER],
        ),
    ],
    Litter: [
        *COMMON_ENTITIES,
        PetKitNumberDesc(
            key="Cleaning Delay",
            translation_key="cleaning_delay",
            entity_category=EntityCategory.CONFIG,
            native_min_value=0,
            native_max_value=60,
            native_step=1,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            mode=NumberMode.SLIDER,
            native_value=lambda device: device.settings.still_time / 60,
            action=lambda api, device, value: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"stillTime": int(value * 60)}
            ),
        ),
    ],
    WaterFountain: [*COMMON_ENTITIES],
    Purifier: [*COMMON_ENTITIES],
    Pet: [
        *COMMON_ENTITIES,
        PetKitNumberDesc(
            key="Pet weight",
            translation_key="pet_weight",
            entity_picture=lambda pet: pet.avatar,
            native_min_value=1,
            native_max_value=100,
            native_step=0.1,
            native_unit_of_measurement=UnitOfMass.KILOGRAMS,
            device_class=NumberDeviceClass.WEIGHT,
            mode=NumberMode.BOX,
            native_value=lambda device: device.pet_details.weight,
            action=lambda api, device, value: api.send_api_request(
                device.id, PetCommand.PET_UPDATE_SETTING, {"weight": int(value)}
            ),
            only_for_types=[PET],
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
        PetkitNumber(
            coordinator=entry.runtime_data.coordinator,
            entity_description=entity_description,
            device=device,
        )
        for device in devices
        for device_type, entity_descriptions in NUMBER_MAPPING.items()
        if isinstance(device, device_type)
        for entity_description in entity_descriptions
        if entity_description.is_supported(device)  # Check if the entity is supported
    ]
    async_add_entities(entities)


class PetkitNumber(PetkitEntity, NumberEntity):
    """Petkit Smart Devices Number class."""

    entity_description: PetKitNumberDesc

    def __init__(
        self,
        coordinator: PetkitDataUpdateCoordinator,
        entity_description: PetKitNumberDesc,
        device: Feeder | Litter | WaterFountain | Purifier | Pet,
    ) -> None:
        """Initialize the switch class."""
        super().__init__(coordinator, device)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self.device = device

    @property
    def unique_id(self) -> str:
        """Return a unique ID for the binary_sensor."""
        return f"{self.device.device_nfo.device_type}_{self.device.sn}_{self.entity_description.key}"

    @property
    def entity_picture(self) -> str | None:
        """Grab associated pet picture."""
        if self.entity_description.entity_picture:
            return self.entity_description.entity_picture(self.device)
        return None

    @property
    def mode(self) -> NumberMode:
        """Return slider mode."""

        return self.entity_description.mode

    @property
    def native_min_value(self) -> float | None:
        """Return minimum allowed value."""

        return self.entity_description.native_min_value

    @property
    def native_max_value(self) -> float | None:
        """Return max value allowed."""

        return self.entity_description.native_max_value

    @property
    def native_step(self) -> float | None:
        """Return stepping by 1."""

        return self.entity_description.native_step

    @property
    def native_value(self) -> float | None:
        """Always reset to native_value"""
        device_data = self.coordinator.data.get(self.device.id)
        if device_data:
            return self.entity_description.native_value(device_data)
        return None

    @property
    def available(self) -> bool:
        """Return if this button is available or not"""
        device_data = self.coordinator.data.get(self.device.id)
        if (
            device_data
            and hasattr(device_data, "state")
            and hasattr(device_data.state, "pim")
        ):
            return device_data.state.pim in POWER_ONLINE_STATE
        return True

    async def async_set_native_value(self, value: str) -> None:
        """Set manual feeding amount."""
        LOGGER.debug(
            "Setting value for : %s with value : %s", self.entity_description.key, value
        )
        await self.entity_description.action(
            self.coordinator.config_entry.runtime_data.client, self.device, value
        )
