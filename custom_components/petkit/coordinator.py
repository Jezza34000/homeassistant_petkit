"""DataUpdateCoordinator for Petkit Smart Devices."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from pypetkitapi import Feeder, Litter, Pet, Purifier, PypetkitError, WaterFountain

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import PetkitConfigEntry


class PetkitDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    config_entry: PetkitConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
            always_update=True,
        )

    async def _async_update_data(
        self,
    ) -> dict[int, Feeder | Litter | WaterFountain | Purifier | Pet]:
        """Update data via library."""
        try:
            await self.config_entry.runtime_data.client.get_devices_data()
        except PypetkitError as exception:
            raise UpdateFailed(exception) from exception
        else:
            return self.config_entry.runtime_data.client.petkit_entities
