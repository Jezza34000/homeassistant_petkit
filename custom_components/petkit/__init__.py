"""Custom integration to integrate Petkit Smart Devices with Home Assistant."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from pypetkitapi import PetKitClient

from homeassistant.const import (
    CONF_PASSWORD,
    CONF_REGION,
    CONF_SCAN_INTERVAL,
    CONF_TIME_ZONE,
    CONF_USERNAME,
    Platform,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_loaded_integration

from .config_flow import PetkitFlowHandler
from .const import (
    BT_SECTION,
    CONF_BLE_RELAY_ENABLED,
    CONF_DELETE_AFTER,
    CONF_MEDIA_DL_IMAGE,
    CONF_MEDIA_DL_VIDEO,
    CONF_MEDIA_EV_TYPE,
    CONF_MEDIA_PATH,
    CONF_SCAN_INTERVAL_BLUETOOTH,
    CONF_SCAN_INTERVAL_MEDIA,
    CONF_SMART_POLLING,
    COORDINATOR,
    COORDINATOR_BLUETOOTH,
    COORDINATOR_MEDIA,
    DEFAULT_BLUETOOTH_RELAY,
    DEFAULT_DELETE_AFTER,
    DEFAULT_DL_IMAGE,
    DEFAULT_DL_VIDEO,
    DEFAULT_EVENTS,
    DEFAULT_MEDIA_PATH,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_BLUETOOTH,
    DEFAULT_SCAN_INTERVAL_MEDIA,
    DEFAULT_SMART_POLLING,
    DOMAIN,
    LOGGER,
    MEDIA_SECTION,
)
from .coordinator import (
    PetkitBluetoothUpdateCoordinator,
    PetkitDataUpdateCoordinator,
    PetkitMediaUpdateCoordinator,
)
from .data import PetkitData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import PetkitConfigEntry

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.LIGHT,
    Platform.TEXT,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.IMAGE,
    Platform.FAN,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
) -> bool:
    """Set up this integration using UI."""

    country_from_ha = hass.config.country
    tz_from_ha = hass.config.time_zone

    coordinator = PetkitDataUpdateCoordinator(
        hass=hass,
        logger=LOGGER,
        name=f"{DOMAIN}.devices",
        update_interval=timedelta(seconds=entry.options[CONF_SCAN_INTERVAL]),
        config_entry=entry,
    )
    coordinator_media = PetkitMediaUpdateCoordinator(
        hass=hass,
        logger=LOGGER,
        name=f"{DOMAIN}.medias",
        update_interval=timedelta(
            minutes=entry.options[MEDIA_SECTION][CONF_SCAN_INTERVAL_MEDIA]
        ),
        config_entry=entry,
        data_coordinator=coordinator,
    )
    coordinator_bluetooth = PetkitBluetoothUpdateCoordinator(
        hass=hass,
        logger=LOGGER,
        name=f"{DOMAIN}.bluetooth",
        update_interval=timedelta(
            minutes=entry.options[BT_SECTION][CONF_SCAN_INTERVAL_BLUETOOTH]
        ),
        config_entry=entry,
        data_coordinator=coordinator,
    )
    entry.runtime_data = PetkitData(
        client=PetKitClient(
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            region=entry.data.get(CONF_REGION, country_from_ha),
            timezone=entry.data.get(CONF_TIME_ZONE, tz_from_ha),
            session=async_get_clientsession(hass),
        ),
        integration=async_get_loaded_integration(hass, entry.domain),
        coordinator=coordinator,
        coordinator_media=coordinator_media,
        coordinator_bluetooth=coordinator_bluetooth,
    )

    await coordinator.async_config_entry_first_refresh()
    await coordinator_media.async_config_entry_first_refresh()
    await coordinator_bluetooth.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    hass.data[DOMAIN][COORDINATOR] = coordinator
    hass.data[DOMAIN][COORDINATOR_MEDIA] = coordinator
    hass.data[DOMAIN][COORDINATOR_BLUETOOTH] = coordinator

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


async def async_update_options(hass: HomeAssistant, entry: PetkitConfigEntry) -> None:
    """Update options."""

    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: PetkitConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    return True


async def async_migrate_entry(
    hass: HomeAssistant, config_entry: PetkitConfigEntry
) -> bool:
    """Migrate old entry."""
    LOGGER.debug(
        "Migrating configuration from version %s.%s",
        config_entry.version,
        config_entry.minor_version,
    )

    data = config_entry.data.copy()
    options = config_entry.options.copy()

    match config_entry.version:
        case 1:  # our pre-migratable
            pass

        case 6:  # latest RobertD502's
            data[CONF_USERNAME] = data.pop("email")
            data[CONF_TIME_ZONE] = options.pop("timezone")

            options[CONF_SCAN_INTERVAL] = options.pop(
                "polling_interval", DEFAULT_SCAN_INTERVAL
            )
            options[CONF_SMART_POLLING] = DEFAULT_SMART_POLLING

            options[BT_SECTION] = {
                CONF_BLE_RELAY_ENABLED: options.pop(
                    "use_ble_relay", DEFAULT_BLUETOOTH_RELAY
                ),
                CONF_SCAN_INTERVAL_BLUETOOTH: DEFAULT_SCAN_INTERVAL_BLUETOOTH,
            }

            options[MEDIA_SECTION] = {
                CONF_MEDIA_PATH: DEFAULT_MEDIA_PATH,
                CONF_SCAN_INTERVAL_MEDIA: DEFAULT_SCAN_INTERVAL_MEDIA,
                CONF_MEDIA_DL_IMAGE: DEFAULT_DL_IMAGE,
                CONF_MEDIA_DL_VIDEO: DEFAULT_DL_VIDEO,
                CONF_MEDIA_EV_TYPE: DEFAULT_EVENTS,
                CONF_DELETE_AFTER: DEFAULT_DELETE_AFTER,
            }

        case _:
            return False

    hass.config_entries.async_update_entry(
        config_entry,
        data=data,
        options=options,
        version=PetkitFlowHandler.VERSION,
        minor_version=PetkitFlowHandler.MINOR_VERSION,
    )

    LOGGER.debug(
        "Migration to configuration version %s.%s successful",
        config_entry.version,
        config_entry.minor_version,
    )
    return True
