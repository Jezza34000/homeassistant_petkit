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
    BT_SECTION,
    CONF_BLE_RELAY_ENABLED,
    CONF_DELETE_AFTER,
    CONF_MEDIA_DL_IMAGE,
    CONF_MEDIA_DL_VIDEO,
    CONF_MEDIA_EV_TYPE,
    CONF_MEDIA_PATH,
    DEFAULT_BLUETOOTH_RELAY,
    DEFAULT_DELETE_AFTER,
    DEFAULT_DL_IMAGE,
    DEFAULT_DL_VIDEO,
    DEFAULT_EVENTS,
    DEFAULT_MEDIA_PATH,
    DEFAULT_SCAN_INTERVAL,
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
        self.current_devices = set()
        self.fast_poll_tic = 0
        self.mqtt_connected = False

    def enable_smart_polling(self, nb_tic: int) -> None:
        """Enable smart polling."""
        if self.fast_poll_tic > 0:
            LOGGER.debug(
                "Fast poll tic already enabled for %s tics", self.fast_poll_tic
            )
            return

        self.update_interval = timedelta(seconds=SCAN_INTERVAL_FAST)
        self.fast_poll_tic = nb_tic
        LOGGER.debug(
            "Fast poll tic enabled for %s tics (at %ssec interval)",
            nb_tic,
            SCAN_INTERVAL_FAST,
        )

    async def _update_smart_polling(self) -> None:
        """Update smart polling."""
        if self.fast_poll_tic > 0:
            self.fast_poll_tic -= 1
            LOGGER.debug("Fast poll tic remaining: %s", self.fast_poll_tic)
        else:
            base_interval = (
                SCAN_INTERVAL_SLOW if self.mqtt_connected else DEFAULT_SCAN_INTERVAL
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


class PetkitLocalBleCoordinator(DataUpdateCoordinator):
    """Coordinator for direct (local) BLE communication with PetKit fountains.

    Polls each configured fountain over direct Bluetooth at a fixed interval.
    Updates the WaterFountain entity data in the main data coordinator so that
    existing entities automatically reflect BLE-sourced state without needing
    separate BLE-specific entities.
    """

    def __init__(
        self,
        hass,
        logger,
        name,
        update_interval,
        config_entry,
        data_coordinator: PetkitDataUpdateCoordinator,
    ):
        """Initialize the local BLE coordinator."""

        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=update_interval,
            config_entry=config_entry,
        )
        self.config = config_entry
        self.data_coordinator = data_coordinator

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll all configured local BLE fountains and update entity data."""
        from .const import (
            BLE_CONNECT_GRACE_SECONDS,
            CONF_BLE_SECRETS,
            CONF_LOCAL_BLE_DEBUG,
            CONF_LOCAL_BLE_FOUNTAINS,
            DEFAULT_LOCAL_BLE_DEBUG,
            LOCAL_BLE_SECTION,
        )
        from .fountain_ble import FountainBleClient, LocalFountainBleProtocol
        from .utils import is_local_ble_active, normalize_mac

        if not is_local_ble_active(self.config.options):
            LOGGER.debug("Local BLE is disabled by configuration (cloud_only mode)")
            return {}

        ble_section = self.config.options.get(LOCAL_BLE_SECTION, {})
        debug_log: bool = ble_section.get(CONF_LOCAL_BLE_DEBUG, DEFAULT_LOCAL_BLE_DEBUG)
        fountains: list[dict] = ble_section.get(CONF_LOCAL_BLE_FOUNTAINS, [])
        # Per-device BLE secret cache (Jezza34000 PR #203 design 2026-05-02).
        secrets_cache: dict[str, str] = dict(self.config.data.get(CONF_BLE_SECRETS, {}))
        results: dict[str, Any] = {}

        def _make_persist_cb(self_ref: PetkitLocalBleCoordinator) -> Any:
            async def _persist(mac: str, secret_hex: str) -> None:
                key = normalize_mac(mac) or mac
                if secrets_cache.get(key) == secret_hex:
                    return
                secrets_cache[key] = secret_hex
                new_data = {
                    **self_ref.config.data,
                    CONF_BLE_SECRETS: dict(secrets_cache),
                }
                self_ref.hass.config_entries.async_update_entry(
                    self_ref.config, data=new_data
                )

            return _persist

        persist_cb = _make_persist_cb(self)

        for fountain_cfg in fountains:
            mac: str = fountain_cfg.get("mac", "")
            name: str = fountain_cfg.get("name", mac)
            if not mac:
                continue
            cached_hex = secrets_cache.get(normalize_mac(mac) or mac)
            cached_secret: list[int] | None = None
            if cached_hex:
                try:
                    cached_secret = list(bytes.fromhex(cached_hex))
                    if len(cached_secret) != 8:
                        cached_secret = None
                except ValueError:
                    cached_secret = None
            try:
                client = FountainBleClient(
                    self.hass,
                    mac,
                    name,
                    debug_log=debug_log,
                    cached_secret=cached_secret,
                    secret_persist_cb=persist_cb,
                    connect_grace_seconds=BLE_CONNECT_GRACE_SECONDS,
                )
                status = await client.async_get_status()
                if status is None:
                    LOGGER.warning("No BLE status received from %s (%s)", name, mac)
                    continue
                self._apply_ble_update_to_entities(
                    mac, name, status, results, LocalFountainBleProtocol
                )
            except Exception as err:  # noqa: BLE001
                LOGGER.error("Local BLE poll failed for %s (%s): %s", name, mac, err)

        if results:
            # Notify all entities subscribed to the main data coordinator so
            # they pick up the in-place BLE changes without waiting for the
            # next cloud poll.
            self.data_coordinator.async_update_listeners()

        return results

    def _apply_ble_update_to_entities(
        self,
        mac: str,
        name: str,
        status: dict[str, Any],
        results: dict[str, Any],
        protocol_cls: Any,
    ) -> None:
        """Find the matching WaterFountain entity and apply BLE status to it.

        MAC compare is case- and separator-insensitive: the user-typed value
        in options (e.g. ``A4:C1:38:99:18:D4``) must match the canonical
        device representation (``a4c1389918d4``). (fredrik-lindseth, PR #203
        follow-up #2.)
        """
        from pypetkitapi import WaterFountain

        from .utils import normalize_mac

        target = normalize_mac(mac)
        if not target:
            return

        for device in self.config.runtime_data.client.petkit_entities.values():
            if not isinstance(device, WaterFountain):
                continue
            for attr in ("ble_mac", "mac"):
                dev_mac = normalize_mac(getattr(device, attr, None))
                if dev_mac and dev_mac == target:
                    protocol_cls.update_water_fountain(device, status)
                    results[mac] = status
                    LOGGER.debug("Local BLE update applied for %s (%s)", name, mac)
                    self._bump_last_ble_connection(device)
                    return

        # No matched entity — store raw status anyway
        results[mac] = status

    def _bump_last_ble_connection(self, device: Any) -> None:
        """Mirror successful local-BLE poll into the cloud-relay coordinator.

        ``sensor.last_ble_connection`` reads ``coordinator_bluetooth.data``,
        which only the cloud-relay coordinator writes. Bumping the same dict
        from the local-BLE path keeps that sensor accurate regardless of
        which path delivered the latest update. (fredrik-lindseth, PR #203
        follow-up #3.)
        """
        runtime = getattr(self.config, "runtime_data", None)
        cb_coord = getattr(runtime, "coordinator_bluetooth", None) if runtime else None
        if cb_coord is None:
            return
        ts_dict = getattr(cb_coord, "last_update_timestamps", None)
        if ts_dict is None:
            return
        ts_dict[device.id] = datetime.now(timezone.utc)
        cb_coord.async_set_updated_data(ts_dict)
