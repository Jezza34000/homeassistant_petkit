"""Image platform for Petkit Smart Devices integration."""

from __future__ import annotations

from dataclasses import dataclass
import datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import aiofiles
from pypetkitapi import (
    FEEDER_WITH_CAMERA,
    LITTER_WITH_CAMERA,
    Feeder,
    Litter,
    Pet,
    WaterFountain,
)

from homeassistant.components.image import ImageEntity, ImageEntityDescription
from homeassistant.core import callback

from .const import CONF_MEDIA_DL_IMAGE, LOGGER, MEDIA_SECTION
from .entity import PetKitDescSensorBase, PetkitEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import PetkitMediaUpdateCoordinator
    from .data import PetkitConfigEntry, PetkitDevices


@dataclass(frozen=True, kw_only=True)
class PetKitImageDesc(PetKitDescSensorBase, ImageEntityDescription):
    """A class that describes sensor entities."""

    event_key: str | None = None  # Event key to get the image from


COMMON_ENTITIES = []

IMAGE_MAPPING: dict[type[PetkitDevices], list[PetKitImageDesc]] = {
    Feeder: [
        *COMMON_ENTITIES,
        PetKitImageDesc(
            key="Last visit event",
            event_key="pet",
            translation_key="last_visit_event",
            only_for_types=FEEDER_WITH_CAMERA,
        ),
        PetKitImageDesc(
            key="Last eat event",
            event_key="eat",
            translation_key="last_eat_event",
            only_for_types=FEEDER_WITH_CAMERA,
        ),
        PetKitImageDesc(
            key="Last feed event",
            event_key="feed",
            translation_key="last_feed_event",
            only_for_types=FEEDER_WITH_CAMERA,
        ),
        PetKitImageDesc(
            key="Dish before",
            event_key="dish_before",
            translation_key="dish_before",
            only_for_types=FEEDER_WITH_CAMERA,
        ),
        PetKitImageDesc(
            key="Dish after",
            event_key="dish_after",
            translation_key="dish_after",
            only_for_types=FEEDER_WITH_CAMERA,
        ),
    ],
    Litter: [
        *COMMON_ENTITIES,
        PetKitImageDesc(
            key="Last usage event",
            event_key="toileting",
            translation_key="last_toileting_event",
            only_for_types=LITTER_WITH_CAMERA,
        ),
        PetKitImageDesc(
            key="Last visit event",
            event_key="pet",
            translation_key="last_visit_event",
            only_for_types=LITTER_WITH_CAMERA,
        ),
        PetKitImageDesc(
            key="Waste check",
            event_key="waste_check",
            translation_key="waste_check",
            only_for_types=LITTER_WITH_CAMERA,
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
        PetkitImage(
            coordinator=entry.runtime_data.coordinator_media,
            entity_description=entity_description,
            config_entry=entry.options,
            device=device,
        )
        for device in devices
        for device_type, entity_descriptions in IMAGE_MAPPING.items()
        if isinstance(device, device_type)
        for entity_description in entity_descriptions
        if entity_description.is_supported(device)
    ]
    async_add_entities(entities)


class PetkitImage(PetkitEntity, ImageEntity):
    """Petkit Smart Devices Image class."""

    entity_description: PetKitImageDesc

    def __init__(
        self,
        coordinator: PetkitMediaUpdateCoordinator,
        entity_description: PetKitImageDesc,
        config_entry: MappingProxyType[str, Any],
        device: Feeder | Litter | WaterFountain | Pet,
    ) -> None:
        """Initialize the switch class."""
        super().__init__(coordinator, device)
        ImageEntity.__init__(self, coordinator.hass)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self.config_entry = config_entry
        self.device = device
        self.media_list = []
        self._attr_image_last_updated = None
        self._last_image_file: str | None = None
        self.get_last_image()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        super()._handle_coordinator_update()
        self.get_last_image()
        self.async_write_ha_state()

    @property
    def unique_id(self) -> str:
        """Return a unique ID for the binary_sensor."""
        return f"{self.device.device_nfo.device_type}_{self.device.sn}_{self.entity_description.key}"

    @property
    def available(self) -> bool:
        """Return if this button is available or not"""
        if self.config_entry.get(MEDIA_SECTION, {}).get(CONF_MEDIA_DL_IMAGE, False):
            return True
        self._attr_image_last_updated = None
        self._last_image_file = None
        return False

    @callback
    def get_last_image(self):
        """Get the last image filename"""
        event_key = self.entity_description.event_key
        media_table = self.coordinator.media_table

        # Filter media files by device_id and event_key
        matching_media_files = [
            media_file
            for media_file in media_table.get(self.device.id, [])
            if media_file.event_type == event_key
        ]

        if not matching_media_files:
            LOGGER.info(
                f"No media files found for device id = {self.device.id} and event key = {event_key}"
            )
            self._attr_image_last_updated = None
            self._last_image_file = None
            return

        # Find the media file with the most recent timestamp
        latest_media_file = max(
            matching_media_files, key=lambda media_file: media_file.timestamp
        )

        image_path = latest_media_file.full_file_path
        self._attr_image_last_updated = datetime.datetime.fromtimestamp(
            latest_media_file.timestamp
        )
        self._last_image_file = image_path

    async def async_image(self) -> bytes | None:
        """Return bytes of image asynchronously."""
        no_img = Path(__file__).parent / "img" / "no-image.png"

        if not self._last_image_file:
            LOGGER.error("No media files found")
            self._attr_image_last_updated = None
            return await self._read_file(no_img)

        LOGGER.debug(
            f"Getting image for {self.device.device_nfo.device_type} Path is :{self._last_image_file}"
        )
        return await self._read_file(self._last_image_file)

    @staticmethod
    async def _read_file(image_path) -> bytes | None:
        try:
            async with aiofiles.open(image_path, "rb") as image_file:
                return await image_file.read()
        except FileNotFoundError:
            LOGGER.error("Unable to read image file")
            return None
