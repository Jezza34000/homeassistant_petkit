"""Switch platform for Petkit Smart Devices integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pypetkitapi import (
    D3,
    D4,
    D4H,
    D4S,
    D4SH,
    FEEDER,
    FEEDER_MINI,
    Feeder,
    FeederCommand,
    Litter,
    Pet,
    Purifier,
    WaterFountain,
)

from homeassistant.components.text import TextEntity, TextEntityDescription

from .const import LOGGER, POWER_ONLINE_STATE, SCAN_INTERVAL_FAST
from .entity import PetKitDescSensorBase, PetkitEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import PetkitDataUpdateCoordinator
    from .data import PetkitConfigEntry, PetkitDevices


@dataclass(frozen=True, kw_only=True)
class PetkitTextDesc(PetKitDescSensorBase, TextEntityDescription):
    """A class that describes sensor entities."""

    native_value: str | None = None
    action: Callable[[PetkitConfigEntry, PetkitDevices, str], Any] | None = None
    valid_values: Callable[[PetkitDevices], list[int]] | None = None


COMMON_ENTITIES = []


def _valid_manual_feed_values(device: PetkitDevices) -> list[int]:
    """Return supported manual feed amounts for a feeder model."""
    device_type = device.device_nfo.device_type

    if device_type == FEEDER:
        return list(range(0, 401, 20))
    if device_type in [D4, D4H]:
        return [10, 20, 30, 40, 50]
    if device_type == FEEDER_MINI:
        return list(range(0, 51, 5))
    if device_type == D3:
        return list(range(5, 201))
    return [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


TEXT_MAPPING: dict[type[PetkitDevices], list[PetkitTextDesc]] = {
    Feeder: [
        *COMMON_ENTITIES,
        PetkitTextDesc(
            key="Manual feed single",
            translation_key="manual_feed_single",
            native_min=1,
            native_max=3,
            valid_values=_valid_manual_feed_values,
            native_value="0",
            action=lambda api, device, amount_value: api.send_api_request(
                device.id, FeederCommand.MANUAL_FEED, {"amount": int(amount_value)}
            ),
            only_for_types=[FEEDER, FEEDER_MINI, D3, D4, D4H],
        ),
        PetkitTextDesc(
            key="Manual feed dual h1",
            translation_key="manual_feed_dual_h1",
            native_min=1,
            native_max=2,
            valid_values=_valid_manual_feed_values,
            native_value="0",
            action=lambda api, device, amount_value: api.send_api_request(
                device.id,
                FeederCommand.MANUAL_FEED,
                {"amount1": int(amount_value), "amount2": 0},
            ),
            only_for_types=[D4S, D4SH],
        ),
        PetkitTextDesc(
            key="Manual feed dual h2",
            translation_key="manual_feed_dual_h2",
            native_min=1,
            native_max=2,
            valid_values=_valid_manual_feed_values,
            native_value="0",
            action=lambda api, device, amount_value: api.send_api_request(
                device.id,
                FeederCommand.MANUAL_FEED,
                {"amount1": 0, "amount2": int(amount_value)},
            ),
            only_for_types=[D4S, D4SH],
        ),
    ],
    Litter: [*COMMON_ENTITIES],
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
        PetkitText(
            coordinator=entry.runtime_data.coordinator,
            entity_description=entity_description,
            device=device,
        )
        for device in devices
        for device_type, entity_descriptions in TEXT_MAPPING.items()
        if isinstance(device, device_type)
        for entity_description in entity_descriptions
        if entity_description.is_supported(device)  # Check if the entity is supported
    ]
    LOGGER.debug(
        "TEXT : Adding %s (on %s available)",
        len(entities),
        sum(len(descriptors) for descriptors in TEXT_MAPPING.values()),
    )
    async_add_entities(entities)


class PetkitText(PetkitEntity, TextEntity):
    """Petkit Smart Devices Switch class."""

    entity_description: PetkitTextDesc

    def __init__(
        self,
        coordinator: PetkitDataUpdateCoordinator,
        entity_description: PetkitTextDesc,
        device: PetkitDevices,
    ) -> None:
        """Initialize the switch class."""
        super().__init__(coordinator, device)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self.device = device

    @property
    def native_max(self) -> int:
        """Max number of characters."""

        return self.entity_description.native_max

    @property
    def native_min(self) -> int:
        """Min number of characters."""

        return self.entity_description.native_min

    @property
    def pattern(self) -> str | None:
        """Check validity with regex pattern."""

        if self.entity_description.valid_values is not None:
            values = self.entity_description.valid_values(self.device)
            return f"^({'|'.join(str(value) for value in values)})$"
        return self.entity_description.pattern

    @property
    def native_value(self) -> str:
        """Always reset to native_value."""

        return self.entity_description.native_value

    @property
    def available(self) -> bool:
        """Return if this button is available or not."""
        device_data = self.coordinator.data.get(self.device.id)
        if device_data and hasattr(device_data.state, "pim"):
            return device_data.state.pim in POWER_ONLINE_STATE
        return True

    async def async_set_value(self, value: str) -> None:
        """Set manual feeding amount."""

        valid_values = _valid_manual_feed_values(self.device)

        if int(value) not in valid_values:
            raise ValueError(
                f"Feeding value '{value}' is not valid for this feeder. Valid values are: {valid_values}"
            )

        self.coordinator.update_interval = timedelta(seconds=SCAN_INTERVAL_FAST)
        self.coordinator.fast_poll_tic = 3
        LOGGER.debug(
            "Setting value for : %s with value : %s", self.entity_description.key, value
        )
        await self.entity_description.action(
            self.coordinator.config_entry.runtime_data.client, self.device, value
        )
