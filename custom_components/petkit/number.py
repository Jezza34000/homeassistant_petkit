"""Switch platform for Petkit Smart Devices integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    NumberDeviceClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from pypetkitapi.command import FeederCommand, DeviceCommand
from pypetkitapi.const import (
    D3,
    D4,
    D4H,
    D4S,
    D4SH,
    FEEDER,
    FEEDER_MINI,
    DEVICES_FEEDER,
)
from pypetkitapi.feeder_container import Feeder
from pypetkitapi.litter_container import Litter
from pypetkitapi.water_fountain_container import WaterFountain

from .const import LOGGER
from .entity import PetKitDescSensorBase, PetkitEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import PetkitDataUpdateCoordinator
    from .data import PetkitConfigEntry


@dataclass(frozen=True, kw_only=True)
class PetKitNumberDesc(PetKitDescSensorBase, NumberEntityDescription):
    """A class that describes number entities."""

    native_value: Callable[[Feeder | Litter | WaterFountain], float | None] | None = (
        None
    )
    action: (
        Callable[[PetkitConfigEntry, Feeder | Litter | WaterFountain, str], Any] | None
    ) = None


NUMBER_MAPPING: dict[type[Feeder | Litter | WaterFountain], list[PetKitNumberDesc]] = {
    Feeder: [
        PetKitNumberDesc(
            key="Volume",
            translation_key="volume",
            entity_category=EntityCategory.CONFIG,
            native_min_value=1,
            native_max_value=9,
            native_step=1,
            mode=NumberMode.SLIDER,
            native_value=lambda device: device.settings.volume,
            action=lambda api, device, value: api.config_entry.runtime_data.client.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"volume": int(value)}
            ),
            only_for_types=[D3, D4H, D4SH],
        ),
        PetKitNumberDesc(
            key="Surplus",
            translation_key="surplus",
            entity_category=EntityCategory.CONFIG,
            native_min_value=20,
            native_max_value=100,
            native_step=10,
            mode=NumberMode.SLIDER,
            native_value=lambda device: device.settings.surplus,
            action=lambda api, device, value: api.config_entry.runtime_data.client.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"surplus": int(value)}
            ),
            only_for_types=[D3],
        ),
        PetKitNumberDesc(
            key="Manual Feed",
            translation_key="manual_feed",
            entity_category=EntityCategory.CONFIG,
            native_min_value=4,
            native_max_value=200,
            native_step=1,
            device_class=NumberDeviceClass.WEIGHT,
            mode=NumberMode.SLIDER,
            native_value=lambda device: 4,
            action=lambda api, device, value: api.config_entry.runtime_data.client.send_api_request(
                device.id, FeederCommand.MANUAL_FEED, {"amount": int(value)}
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
            action=lambda api, device, value: api.config_entry.runtime_data.client.send_api_request(
                device, DeviceCommand.UPDATE_SETTING, {"shortest": int(value)}
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
            action=lambda api, device, value: api.config_entry.runtime_data.client.send_api_request(
                device, FeederCommand.MANUAL_FEED, {"amount": int(value)}
            ),
            only_for_types=[FEEDER],
        ),
    ],
    Litter: [
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
            action=lambda api, device, value: api.config_entry.runtime_data.client.send_api_request(
                device, DeviceCommand.UPDATE_SETTING, {"stillTime": int(value * 60)}
            ),
        ),
    ],
    WaterFountain: [],
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary_sensors using config entry."""
    devices = entry.runtime_data.client.device_list.values()
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
        device: Feeder | Litter | WaterFountain,
    ) -> None:
        """Initialize the switch class."""
        super().__init__(coordinator, device)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self.device = device

    @property
    def unique_id(self) -> str:
        """Return a unique ID for the binary_sensor."""
        return (
            f"{self.device.device_type}_{self.device.sn}_{self.entity_description.key}"
        )

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

        return self.entity_description.native_value(self.device)

    async def async_set_native_value(self, value: str) -> None:
        """Set manual feeding amount."""
        LOGGER.debug(
            "Setting value for : %s with value : %s", self.entity_description.key, value
        )
        await self.entity_description.action(self.coordinator, self.device, value)