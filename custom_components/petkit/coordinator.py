"""DataUpdateCoordinator for Petkit Smart Devices."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
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
    ADVANCED_POLLING_SECTION,
    BT_SECTION,
    CONF_BLE_RELAY_ENABLED,
    CONF_DELETE_AFTER,
    CONF_MEDIA_DL_IMAGE,
    CONF_MEDIA_DL_VIDEO,
    CONF_MEDIA_EV_TYPE,
    CONF_MEDIA_PATH,
    CONF_SCAN_INTERVAL_DEFAULT,
    CONF_SCAN_INTERVAL_SLOW,
    CONF_SMART_POLLING_BOOST_DURATION,
    DEFAULT_BLUETOOTH_RELAY,
    DEFAULT_DELETE_AFTER,
    DEFAULT_DL_IMAGE,
    DEFAULT_DL_VIDEO,
    DEFAULT_EVENTS,
    DEFAULT_MEDIA_PATH,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SMART_POLLING_BOOST_DURATION,
    DOMAIN,
    LOGGER,
    MEDIA_SECTION,
    SCAN_INTERVAL_FAST,
    SCAN_INTERVAL_SLOW,
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
        self.previous_device_identifiers = {}
        self.current_devices = set()
        self.fast_poll_tic = 0
        self.mqtt_connected = False
        self._reload_polling_options()

    def _reload_polling_options(self) -> None:
        """Read polling intervals from the config entry options.

        Falls back to historical defaults when the section is missing so
        existing entries (and tests that build entries without options)
        keep working without an explicit migration step.
        """
        polling_options = self.config_entry.options.get(ADVANCED_POLLING_SECTION, {})
        self.scan_interval_slow = polling_options.get(
            CONF_SCAN_INTERVAL_SLOW, SCAN_INTERVAL_SLOW
        )
        self.scan_interval_default = polling_options.get(
            CONF_SCAN_INTERVAL_DEFAULT, DEFAULT_SCAN_INTERVAL
        )
        self.smart_polling_boost_duration = polling_options.get(
            CONF_SMART_POLLING_BOOST_DURATION, DEFAULT_SMART_POLLING_BOOST_DURATION
        )

    def enable_smart_polling(self, boost_duration: int | None = None) -> None:
        """Enable smart polling for the configured boost duration.

        Args:
            boost_duration: Override duration in seconds. When ``None`` (the
                default), the coordinator uses the user-configured value from
                ``advanced_polling_options.smart_polling_boost_duration``.

        """
        if self.fast_poll_tic > 0:
            LOGGER.debug(
                "Fast poll tic already enabled for %s tics", self.fast_poll_tic
            )
            return

        if boost_duration is None:
            boost_duration = self.smart_polling_boost_duration

        nb_tic = max(1, boost_duration // SCAN_INTERVAL_FAST)
        self.update_interval = timedelta(seconds=SCAN_INTERVAL_FAST)
        self.fast_poll_tic = nb_tic
        LOGGER.debug(
            "Fast poll tic enabled for %s tics (at %ssec interval, ~%ss total)",
            nb_tic,
            SCAN_INTERVAL_FAST,
            nb_tic * SCAN_INTERVAL_FAST,
        )

    async def _update_smart_polling(self) -> None:
        """Update smart polling."""
        if self.fast_poll_tic > 0:
            self.fast_poll_tic -= 1
            LOGGER.debug("Fast poll tic remaining: %s", self.fast_poll_tic)
        else:
            base_interval = (
                self.scan_interval_slow
                if self.mqtt_connected
                else self.scan_interval_default
            )

            if self.update_interval != timedelta(seconds=base_interval):
                self.update_interval = timedelta(seconds=base_interval)
                LOGGER.debug(
                    "Fast poll tic ended, reset to %s sec interval", base_interval
                )

    async def _async_update_data(
        self,
    ) -> dict[int, Feeder | Litter | WaterFountain | Purifier | Pet]:
        """Update data via library."""
        await self._update_smart_polling()

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
            previous_devices = self.previous_devices
            previous_identifiers = self.previous_device_identifiers
            self.current_devices = set(data)
            self.previous_device_identifiers = {
                device_id: str(getattr(device, "sn", device_id))
                for device_id, device in data.items()
            }

            # Check if there are any stale devices
            if stale_devices := previous_devices - self.current_devices:
                device_registry = dr.async_get(self.hass)
                for device_id in stale_devices:
                    device = device_registry.async_get(
                        identifiers={
                            (DOMAIN, previous_identifiers.get(device_id, device_id))
                        }
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
            # User supplied a full path (e.g. /config or /config/petkit) — use it as-is.
            self.media_path = raw
        else:
            # Relative name (e.g. "petkit") → store under /media for backward compat.
            self.media_path = Path("/media") / raw

        LOGGER.debug("Media path = %s", self.media_path)

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
                LOGGER.debug("Device id = %s does not support medias", device)
                continue

            media_lst = client.petkit_entities[device].medias

            if not media_lst:
                LOGGER.debug("No medias found for device id = %s", device)
                continue

            LOGGER.debug("Gathering medias files onto disk for device id = %s", device)
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
                "Downloaded all medias for device id = %s is OK (got %s files to download)",
                device,
                len(to_dl),
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
                LOGGER.warning("Device media path not found: %s", device_media_path)
                continue

            for date_dir in date_dirs:
                if date_dir.is_dir():
                    try:
                        dir_date = datetime.strptime(date_dir.name, "%Y%m%d")
                        if dir_date < retention_date:
                            LOGGER.debug("Deleting old media files in %s", date_dir)
                            await asyncio.to_thread(shutil.rmtree, date_dir)
                    except ValueError:
                        LOGGER.warning(
                            "Invalid date format in directory name: %s", date_dir.name
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
            LOGGER.debug("Bluetooth connection for device id = %s is OK", device_id)
            self.last_update_timestamps[device_id] = datetime.now(UTC)
            return True
        LOGGER.debug("Bluetooth connection for device id = %s failed", device_id)
        return False
