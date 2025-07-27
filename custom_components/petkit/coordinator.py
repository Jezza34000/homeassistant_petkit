"""DataUpdateCoordinator for Petkit Smart Devices."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
from typing import Any

import aiofiles
import aiofiles.os
from pypetkitapi import (
    DownloadDecryptMedia,
    Feeder,
    Litter,
    MediaFile,
    MediaType,
    Pet,
    PetkitAuthenticationUnregisteredEmailError,
    PetkitRegionalServerNotFoundError,
    PetkitSessionError,
    PetkitSessionExpiredError,
    Purifier,
    PypetkitError,
    RecordType,
    WaterFountain,
)

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BT_SECTION,
    CONF_BLE_RELAY_ENABLED,
    CONF_DELETE_AFTER,
    CONF_MEDIA_DL_IMAGE,
    CONF_MEDIA_DL_VIDEO,
    CONF_MEDIA_EV_TYPE,
    CONF_MEDIA_PATH,
    CONF_SMART_POLLING,
    DEFAULT_BLUETOOTH_RELAY,
    DEFAULT_DELETE_AFTER,
    DEFAULT_DL_IMAGE,
    DEFAULT_DL_VIDEO,
    DEFAULT_EVENTS,
    DEFAULT_MEDIA_PATH,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SMART_POLLING,
    DOMAIN,
    LOGGER,
    MEDIA_SECTION,
    MIN_SCAN_INTERVAL,
)


class PetkitDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass, logger, name, update_interval, config_entry):
        """Initialize the data update coordinator."""
        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=update_interval,
            config_entry=config_entry,
        )
        self.config_entry = config_entry
        self.previous_devices = set()
        self.curent_devices = set()
        self.fast_poll_tic = 0

    def enable_smart_polling(self, nb_tic: int) -> None:
        """Enable smart polling."""
        if self.fast_poll_tic > 0:
            LOGGER.debug(f"Fast poll tic already enabled for {self.fast_poll_tic} tics")
            return

        if not self.config_entry.options.get(CONF_SMART_POLLING, DEFAULT_SMART_POLLING):
            LOGGER.debug("Smart polling is disabled by configuration")
            return

        self.update_interval = timedelta(seconds=MIN_SCAN_INTERVAL)
        self.fast_poll_tic = nb_tic
        LOGGER.debug(
            f"Fast poll tic enabled for {nb_tic} tics (at {MIN_SCAN_INTERVAL}sec interval)"
        )

    async def update_smart_polling(self) -> None:
        """Update smart polling."""
        if self.fast_poll_tic > 0:
            self.fast_poll_tic -= 1
            LOGGER.debug(f"Fast poll tic remaining = {self.fast_poll_tic}")
        elif self.update_interval != timedelta(seconds=DEFAULT_SCAN_INTERVAL):
            self.update_interval = timedelta(seconds=DEFAULT_SCAN_INTERVAL)
            LOGGER.debug("Fast poll tic ended, reset to default scan interval")

    async def _async_update_data(
        self,
    ) -> dict[int, Feeder | Litter | WaterFountain | Purifier | Pet]:
        """Update data via library."""
        await self.update_smart_polling()

        try:
            await self.config_entry.runtime_data.client.get_devices_data()
        except (
            PetkitSessionExpiredError,
            PetkitSessionError,
            PetkitAuthenticationUnregisteredEmailError,
            PetkitRegionalServerNotFoundError,
        ) as exception:
            raise ConfigEntryAuthFailed(exception) from exception
        except PypetkitError as exception:
            raise UpdateFailed(exception) from exception
        else:
            data = self.config_entry.runtime_data.client.petkit_entities
            self.current_devices = set(data)

            # Check if there are any stale devices
            if stale_devices := self.previous_devices - self.current_devices:
                device_registry = dr.async_get(self.hass)
                for device_id in stale_devices:
                    device = device_registry.async_get(
                        identifiers={(DOMAIN, device_id)}
                    )
                    if device:
                        device_registry.async_update_device(
                            device_id=device.id,
                            remove_config_entry_id=self.config_entry.entry_id,
                        )
                self.previous_devices = self.current_devices
            return data


class PetkitMediaUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(
        self, hass, logger, name, update_interval, config_entry, data_coordinator
    ):
        """Initialize the data update coordinator."""
        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=update_interval,
            config_entry=config_entry,
        )
        self.config_entry = config_entry
        self.data_coordinator = data_coordinator
        self.media_type = []
        self.event_type = []
        self.previous_devices = set()
        self.media_table = {}
        self.delete_after = 0
        self.media_path = Path()
        # Load configuration
        self._get_media_config(config_entry.options)

    def _get_media_config(self, options) -> None:
        """Get media configuration."""
        media_options = options.get(MEDIA_SECTION, {})
        event_type_config = media_options.get(CONF_MEDIA_EV_TYPE, DEFAULT_EVENTS)
        dl_image = media_options.get(CONF_MEDIA_DL_IMAGE, DEFAULT_DL_IMAGE)
        dl_video = media_options.get(CONF_MEDIA_DL_VIDEO, DEFAULT_DL_VIDEO)
        self.delete_after = media_options.get(CONF_DELETE_AFTER, DEFAULT_DELETE_AFTER)
        self.event_type = [RecordType(element.lower()) for element in event_type_config]

        raw = Path(media_options.get(CONF_MEDIA_PATH, DEFAULT_MEDIA_PATH))
        if raw.is_absolute():
            raw = raw.relative_to(raw.anchor)

        self.media_path = Path("/media") / raw

        LOGGER.debug(f"Media path = {self.media_path}")

        if dl_image:
            self.media_type.append(MediaType.IMAGE)
        if dl_video:
            self.media_type.append(MediaType.VIDEO)

    async def _async_update_data(
        self,
    ) -> dict[str, list[MediaFile]]:
        """Update data via library."""

        self.hass.async_create_task(
            self._async_update_media_files(self.data_coordinator.current_devices)
        )
        return self.media_table

    async def _async_update_media_files(self, devices_lst: set) -> None:
        """Update media files."""
        client = self.config_entry.runtime_data.client

        for device in devices_lst:
            if not hasattr(client.petkit_entities[device], "medias"):
                LOGGER.debug(f"Device id = {device} does not support medias")
                continue

            media_lst = client.petkit_entities[device].medias

            if not media_lst:
                LOGGER.debug(f"No medias found for device id = {device}")
                continue

            LOGGER.debug(f"Gathering medias files onto disk for device id = {device}")
            await client.media_manager.gather_all_media_from_disk(
                self.media_path, device
            )
            to_dl = await client.media_manager.list_missing_files(
                media_lst, self.media_type, self.event_type
            )

            dl_mgt = DownloadDecryptMedia(self.media_path, client)
            for media in to_dl:
                await dl_mgt.download_file(media, self.media_type)
            LOGGER.debug(
                f"Downloaded all medias for device id = {device} is OK (got {len(to_dl)} files to download)"
            )
            self.media_table[device] = deepcopy(
                await client.media_manager.gather_all_media_from_disk(
                    self.media_path, device
                )
            )
        LOGGER.debug("Update media files finished for all devices")
        await self._async_delete_old_media()

    async def _async_delete_old_media(self) -> None:
        """Delete old media files based on the retention policy."""
        if self.delete_after == 0:
            LOGGER.debug("Media deletion is disabled by configuration")
            return

        retention_date = datetime.now() - timedelta(days=self.delete_after)

        for device_id in self.data_coordinator.current_devices:
            device_media_path = self.media_path / str(device_id)
            if not await aiofiles.os.path.exists(str(device_media_path)):
                continue

            def list_directory(path):
                return list(path.iterdir())

            try:
                date_dirs = await asyncio.to_thread(list_directory, device_media_path)
            except FileNotFoundError:
                LOGGER.warning(f"Device media path not found: {device_media_path}")
                continue

            for date_dir in date_dirs:
                if date_dir.is_dir():
                    try:
                        dir_date = datetime.strptime(date_dir.name, "%Y%m%d")
                        if dir_date < retention_date:
                            LOGGER.debug(f"Deleting old media files in {date_dir}")
                            await asyncio.to_thread(shutil.rmtree, date_dir)
                    except ValueError:
                        LOGGER.warning(
                            f"Invalid date format in directory name: {date_dir.name}"
                        )


class PetkitBluetoothUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage bluetooth connection for Petkit Smart Devices."""

    def __init__(
        self, hass, logger, name, update_interval, config_entry, data_coordinator
    ):
        """Initialize the data update coordinator."""
        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=update_interval,
            config_entry=config_entry,
        )
        self.config = config_entry
        self.data_coordinator = data_coordinator
        self.last_update_timestamps = {}

    async def _async_update_data(
        self,
    ) -> dict[int, Any]:
        """Update data via connecting to bluetooth (over API)."""
        updated_fountain = {}

        if not self.config.options.get(BT_SECTION, {}).get(
            CONF_BLE_RELAY_ENABLED, DEFAULT_BLUETOOTH_RELAY
        ):
            LOGGER.debug("BLE relay is disabled by configuration")
            return updated_fountain

        LOGGER.debug("Update bluetooth connection for all fountains")
        for device_id in self.data_coordinator.current_devices:
            device = self.config.runtime_data.client.petkit_entities.get(device_id)
            if isinstance(device, WaterFountain):
                LOGGER.debug(
                    f"Updating bluetooth connection for device id = {device_id}"
                )
                self.hass.async_create_task(
                    self._async_update_bluetooth_connection(device_id)
                )
        return self.last_update_timestamps

    async def _async_update_bluetooth_connection(self, device_id: str) -> bool:
        """Update bluetooth connection."""
        if await self.config.runtime_data.client.bluetooth_manager.open_ble_connection(
            device_id
        ):
            await asyncio.sleep(5)
            await self.config.runtime_data.client.bluetooth_manager.close_ble_connection(
                device_id
            )
            LOGGER.debug(f"Bluetooth connection for device id = {device_id} is OK")
            self.last_update_timestamps[device_id] = datetime.now(timezone.utc)
            return True
        LOGGER.debug(f"Bluetooth connection for device id = {device_id} failed")
        return False
