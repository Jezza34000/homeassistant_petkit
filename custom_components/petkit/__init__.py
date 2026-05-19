"""Custom integration to integrate Petkit Smart Devices with Home Assistant."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from pypetkitapi import Feeder, PetKitClient
from pypetkitapi.command import FeederCommand, LitterCommand, PetCommand
import voluptuous as vol

from homeassistant.const import (
    CONF_PASSWORD,
    CONF_REGION,
    CONF_TIME_ZONE,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_loaded_integration

from .const import (
    BT_SECTION,
    CONF_BLE_RELAY_ENABLED,
    CONF_DELETE_AFTER,
    CONF_ENABLED_NOTIFICATIONS,
    CONF_MEDIA_DL_IMAGE,
    CONF_MEDIA_DL_VIDEO,
    CONF_MEDIA_EV_TYPE,
    CONF_MEDIA_PATH,
    CONF_SCAN_INTERVAL_BLUETOOTH,
    CONF_SCAN_INTERVAL_MEDIA,
    COORDINATOR,
    COORDINATOR_BLUETOOTH,
    COORDINATOR_MEDIA,
    DEFAULT_BLUETOOTH_RELAY,
    DEFAULT_DELETE_AFTER,
    DEFAULT_DL_IMAGE,
    DEFAULT_DL_VIDEO,
    DEFAULT_ENABLED_NOTIFICATIONS,
    DEFAULT_EVENTS,
    DEFAULT_MEDIA_PATH,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_BLUETOOTH,
    DEFAULT_SCAN_INTERVAL_MEDIA,
    DOMAIN,
    LOGGER,
    MEDIA_SECTION,
    NOTIFICATION_SECTION,
)
from .coordinator import (
    PetkitBluetoothUpdateCoordinator,
    PetkitDataUpdateCoordinator,
    PetkitMediaUpdateCoordinator,
)
from .data import PetkitData
from .iot_mqtt import PetkitIotMqttListener
from .notifications import PetkitNotificationManager
from .whep_proxy import (
    PetkitDirectWhepProxySessionView,
    PetkitDirectWhepProxyView,
    PetkitUpstreamWhepSessionView,
    PetkitUpstreamWhepView,
    async_cleanup_whep_proxy_sessions,
)

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
    Platform.CAMERA,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.IMAGE,
    Platform.FAN,
]

SERVICE_SET_FEEDING_SCHEDULE = "set_feeding_schedule"
SERVICE_UPDATE_LITTER_BOX_USAGE_RECORD = "update_litter_box_usage_record"
SERVICE_UPDATE_PET_WEIGHT = "update_pet_weight"

FEED_ITEM_SCHEMA = vol.Schema(
    {
        vol.Required("time"): vol.All(int, vol.Range(min=0)),
        vol.Required("name"): cv.string,
        vol.Optional("amount", default=0): vol.Coerce(int),
        vol.Optional("amount1", default=0): vol.Coerce(int),
        vol.Optional("amount2", default=0): vol.Coerce(int),
    }
)

FEED_DAY_SCHEMA = vol.Schema(
    {
        vol.Required("repeats"): vol.Any(cv.positive_int, cv.string),
        vol.Required("items"): vol.All(cv.ensure_list, [FEED_ITEM_SCHEMA]),
        vol.Optional("suspended", default=0): vol.Coerce(int),
    }
)

SERVICE_SET_FEEDING_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.Coerce(int),
        vol.Required("feed_daily_list"): vol.All(cv.ensure_list, [FEED_DAY_SCHEMA]),
    }
)

SERVICE_UPDATE_LITTER_BOX_USAGE_RECORD_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.Coerce(int),
        vol.Required("old_pet_id"): cv.string,
        vol.Required("new_pet_id"): cv.string,
        vol.Required("time_out"): vol.Coerce(int),
    }
)

SERVICE_UPDATE_PET_WEIGHT_SCHEMA = vol.Schema(
    {
        vol.Required("pet_id"): vol.Coerce(int),
        vol.Required("weight"): vol.Coerce(int),
        vol.Optional("unit", default="g"): vol.In(["g", "lbs"]),
    }
)


def _build_feed_daily_list(feed_daily_list: list[dict]) -> list[dict]:
    """Transform the user-friendly service call data into the Petkit API format.

    Adds computed fields (count, totalAmount, totalAmount1, totalAmount2) and
    normalizes each feed item to include all required API fields with defaults.
    """
    result = []
    for day in feed_daily_list:
        items = []
        total_amount = 0
        total_amount1 = 0
        total_amount2 = 0
        for item in day["items"]:
            amount = item.get("amount", 0)
            amount1 = item.get("amount1", 0)
            amount2 = item.get("amount2", 0)
            total_amount += amount
            total_amount1 += amount1
            total_amount2 += amount2
            items.append(
                {
                    "amount": amount,
                    "amount1": amount1,
                    "amount2": amount2,
                    "deviceId": 0,
                    "deviceType": 0,
                    "id": item["time"],
                    "name": item["name"],
                    "petAmount": [],
                    "time": item["time"],
                }
            )
        result.append(
            {
                "count": len(items),
                "items": items,
                "repeats": str(day["repeats"]),
                "suspended": day.get("suspended", 0),
                "totalAmount": total_amount,
                "totalAmount1": total_amount1,
                "totalAmount2": total_amount2,
            }
        )
    return result


async def _async_handle_set_feeding_schedule(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the set_feeding_schedule service call."""
    device_id = call.data["device_id"]
    feed_daily_list = call.data["feed_daily_list"]

    # Find the client that owns this device
    client: PetKitClient | None = None
    for entry in hass.config_entries.async_entries(DOMAIN):
        if hasattr(entry, "runtime_data") and entry.runtime_data:
            candidate = entry.runtime_data.client
            if device_id in candidate.petkit_entities:
                device = candidate.petkit_entities[device_id]
                if isinstance(device, Feeder):
                    client = candidate
                    break

    if client is None:
        raise ValueError(
            f"Feeder with device_id {device_id} not found. "
            "Ensure the device_id matches a registered Petkit feeder."
        )

    api_payload = _build_feed_daily_list(feed_daily_list)

    LOGGER.debug(
        "Setting feeding schedule for device %s with %d day(s)",
        device_id,
        len(api_payload),
    )

    await client.send_api_request(device_id, FeederCommand.SAVE_FEED, api_payload)


async def _async_handle_update_litter_box_usage_record(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the update_litter_box_usage_record service call."""
    device_id = call.data["device_id"]
    old_pet_id = call.data["old_pet_id"]
    new_pet_id = call.data["new_pet_id"]
    time_out = call.data["time_out"]

    client: PetKitClient | None = None
    coordinator = None
    for entry in hass.config_entries.async_entries(DOMAIN):
        if hasattr(entry, "runtime_data") and entry.runtime_data:
            candidate = entry.runtime_data.client
            if device_id in candidate.petkit_entities:
                client = candidate
                coordinator = entry.runtime_data.coordinator
                break

    if client is None:
        raise ValueError(f"Device with device_id {device_id} not found.")

    LOGGER.debug(
        "Updating pet usage record: device=%s old_pet=%s new_pet=%s ts=%s",
        device_id,
        old_pet_id,
        new_pet_id,
        time_out,
    )
    await client.send_api_request(
        device_id=int(new_pet_id),
        action=PetCommand.UPDATE_USAGE_RECORD,
        setting={
            "old_pet_id": old_pet_id,
            "device_id": device_id,
            "time_out": time_out,
        },
    )

    # Refresh coordinator data
    if coordinator is not None:
        LOGGER.debug("Requesting coordinator refresh after pet usage update")
        await coordinator.async_request_refresh()


async def _async_handle_update_pet_weight(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the update_pet_weight service call."""
    pet_id = call.data["pet_id"]
    weight_raw = call.data["weight"]
    unit = call.data["unit"]
    weight = round(weight_raw * 453.592) if unit == "lbs" else weight_raw

    client: PetKitClient | None = None
    for entry in hass.config_entries.async_entries(DOMAIN):
        if hasattr(entry, "runtime_data") and entry.runtime_data:
            candidate = entry.runtime_data.client
            if pet_id in candidate.petkit_entities:
                client = candidate
                break

    if client is None:
        raise ValueError(f"Pet with pet_id {pet_id} not found.")

    LOGGER.debug(
        "Updating weight for pet %s: %s %s → %s g", pet_id, weight_raw, unit, weight
    )
    await client.send_api_request(
        device_id=pet_id,
        action=PetCommand.PET_UPDATE_SETTING,
        setting={"weight": str(weight)},
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
) -> bool:
    """Set up this integration using UI."""

    # Register API views once (idempotent — HA deduplicates by name)
    hass.http.register_view(PetkitDirectWhepProxyView())
    hass.http.register_view(PetkitDirectWhepProxySessionView())
    hass.http.register_view(PetkitUpstreamWhepView())
    hass.http.register_view(PetkitUpstreamWhepSessionView())

    country_from_ha = hass.config.country
    tz_from_ha = hass.config.time_zone

    coordinator = PetkitDataUpdateCoordinator(
        hass=hass,
        logger=LOGGER,
        name=f"{DOMAIN}.devices",
        update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        config_entry=entry,
    )
    coordinator_media = PetkitMediaUpdateCoordinator(
        hass=hass,
        logger=LOGGER,
        name=f"{DOMAIN}.medias",
        update_interval=timedelta(
            minutes=entry.options.get(MEDIA_SECTION, {}).get(
                CONF_SCAN_INTERVAL_MEDIA,
                DEFAULT_SCAN_INTERVAL_MEDIA,
            )
        ),
        config_entry=entry,
        data_coordinator=coordinator,
    )
    coordinator_bluetooth = PetkitBluetoothUpdateCoordinator(
        hass=hass,
        logger=LOGGER,
        name=f"{DOMAIN}.bluetooth",
        update_interval=timedelta(
            minutes=entry.options.get(BT_SECTION, {}).get(
                CONF_SCAN_INTERVAL_BLUETOOTH,
                DEFAULT_SCAN_INTERVAL_BLUETOOTH,
            )
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

    # Login succeeded but PetKit returned no devices. Most common cause is
    # that the user logged in with a secondary account that has not yet
    # accepted the Family Management invitation in the PetKit mobile app.
    # Without this check, the integration silently sets up with zero
    # entities and the user has no idea why.
    if not entry.runtime_data.client.petkit_entities:
        raise ConfigEntryNotReady(
            "PetKit login succeeded, but no devices are shared with this "
            "account. Open the PetKit mobile app, accept the Family "
            "Management invitation for this account, and then reload this "
            "integration."
        )

    await coordinator_media.async_config_entry_first_refresh()
    await coordinator_bluetooth.async_config_entry_first_refresh()

    # MQTT

    mqtt_listener = PetkitIotMqttListener(
        hass=hass,
        client=entry.runtime_data.client,
        coordinator=coordinator,
    )

    entry.runtime_data.mqtt_listener = mqtt_listener
    await mqtt_listener.async_start()

    # Notifications
    enabled_notifications = entry.options.get(NOTIFICATION_SECTION, {}).get(
        CONF_ENABLED_NOTIFICATIONS, DEFAULT_ENABLED_NOTIFICATIONS
    )
    notification_manager = PetkitNotificationManager(
        hass=hass,
        coordinator=coordinator,
        enabled_categories=enabled_notifications,
    )
    await notification_manager.async_start()
    entry.runtime_data.notification_manager = notification_manager

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    hass.data[DOMAIN][COORDINATOR] = coordinator
    hass.data[DOMAIN][COORDINATOR_MEDIA] = coordinator_media
    hass.data[DOMAIN][COORDINATOR_BLUETOOTH] = coordinator_bluetooth

    # Register services (idempotent — only registers once per domain)
    if not hass.services.has_service(DOMAIN, SERVICE_SET_FEEDING_SCHEDULE):

        async def handle_set_feeding_schedule(call: ServiceCall) -> None:
            """Wrapper so HA detects this as a coroutine function."""
            await _async_handle_set_feeding_schedule(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_FEEDING_SCHEDULE,
            handle_set_feeding_schedule,
            schema=SERVICE_SET_FEEDING_SCHEDULE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_UPDATE_LITTER_BOX_USAGE_RECORD):

        async def handle_update_litter_box_usage_record(call: ServiceCall) -> None:
            """Wrapper so HA detects this as a coroutine function."""
            await _async_handle_update_litter_box_usage_record(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_UPDATE_LITTER_BOX_USAGE_RECORD,
            handle_update_litter_box_usage_record,
            schema=SERVICE_UPDATE_LITTER_BOX_USAGE_RECORD_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_UPDATE_PET_WEIGHT):

        async def handle_update_pet_weight(call: ServiceCall) -> None:
            """Wrapper so HA detects this as a coroutine function."""
            await _async_handle_update_pet_weight(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_UPDATE_PET_WEIGHT,
            handle_update_pet_weight,
            schema=SERVICE_UPDATE_PET_WEIGHT_SCHEMA,
        )

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    mqtt_listener = getattr(entry.runtime_data, "mqtt_listener", None)
    if mqtt_listener is not None:
        await mqtt_listener.async_stop()

    await async_cleanup_whep_proxy_sessions(hass)

    notification_manager = getattr(entry.runtime_data, "notification_manager", None)
    if notification_manager is not None:
        notification_manager.stop()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: PetkitConfigEntry) -> bool:
    """Migrate config entry between schema versions."""
    LOGGER.debug(
        "Migrating Petkit entry %s from version %s.%s",
        entry.entry_id,
        entry.version,
        getattr(entry, "minor_version", 1),
    )

    if entry.version < 8:
        new_options = dict(entry.options)
        media_section = dict(new_options.get(MEDIA_SECTION, {}))
        media_section.setdefault(CONF_MEDIA_PATH, DEFAULT_MEDIA_PATH)
        media_section.setdefault(CONF_SCAN_INTERVAL_MEDIA, DEFAULT_SCAN_INTERVAL_MEDIA)
        media_section.setdefault(CONF_MEDIA_DL_IMAGE, DEFAULT_DL_IMAGE)
        media_section.setdefault(CONF_MEDIA_DL_VIDEO, DEFAULT_DL_VIDEO)
        media_section.setdefault(CONF_MEDIA_EV_TYPE, DEFAULT_EVENTS)
        media_section.setdefault(CONF_DELETE_AFTER, DEFAULT_DELETE_AFTER)
        new_options[MEDIA_SECTION] = media_section

        bluetooth_section = dict(new_options.get(BT_SECTION, {}))
        bluetooth_section.setdefault(CONF_BLE_RELAY_ENABLED, DEFAULT_BLUETOOTH_RELAY)
        bluetooth_section.setdefault(
            CONF_SCAN_INTERVAL_BLUETOOTH, DEFAULT_SCAN_INTERVAL_BLUETOOTH
        )
        new_options[BT_SECTION] = bluetooth_section

        section = dict(new_options.get(NOTIFICATION_SECTION, {}))
        section.setdefault(
            CONF_ENABLED_NOTIFICATIONS, list(DEFAULT_ENABLED_NOTIFICATIONS)
        )
        new_options[NOTIFICATION_SECTION] = section
        hass.config_entries.async_update_entry(entry, options=new_options, version=8)

    return True


async def async_update_options(hass: HomeAssistant, entry: PetkitConfigEntry) -> None:
    """Update options."""

    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: PetkitConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    return True
