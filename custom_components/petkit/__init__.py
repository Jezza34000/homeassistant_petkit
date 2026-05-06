"""Custom integration to integrate Petkit Smart Devices with Home Assistant."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from pypetkitapi import Feeder, PetKitClient
from pypetkitapi.command import FeederCommand
import voluptuous as vol

from homeassistant.const import (
    CONF_PASSWORD,
    CONF_REGION,
    CONF_TIME_ZONE,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import ServiceCall
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_loaded_integration

from .const import (
    BT_SECTION,
    CONF_BLE_SECRETS,
    CONF_CONNECTION_MODE,
    CONF_ENABLED_NOTIFICATIONS,
    CONF_LOCAL_BLE_ENABLED,
    CONF_SCAN_INTERVAL_BLUETOOTH,
    CONF_SCAN_INTERVAL_MEDIA,
    COORDINATOR,
    COORDINATOR_BLUETOOTH,
    COORDINATOR_LOCAL_BLE,
    COORDINATOR_MEDIA,
    DEFAULT_CONNECTION_MODE,
    DEFAULT_ENABLED_NOTIFICATIONS,
    DEFAULT_LOCAL_BLE_POLL_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOCAL_BLE_SECTION,
    LOGGER,
    MEDIA_SECTION,
    MODE_AUTO,
    NOTIFICATION_SECTION,
)
from .coordinator import (
    PetkitBluetoothUpdateCoordinator,
    PetkitDataUpdateCoordinator,
    PetkitLocalBleCoordinator,
    PetkitMediaUpdateCoordinator,
)
from .data import PetkitData
from .iot_mqtt import PetkitIotMqttListener
from .notifications import PetkitNotificationManager
from .utils import is_local_ble_active
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

    # Local (direct) BLE coordinator — only created when user has enabled
    # local BLE via the new connection_mode option (or legacy boolean).
    local_ble_enabled = is_local_ble_active(entry.options)
    coordinator_local_ble: PetkitLocalBleCoordinator | None = None
    if local_ble_enabled:
        coordinator_local_ble = PetkitLocalBleCoordinator(
            hass=hass,
            logger=LOGGER,
            name=f"{DOMAIN}.local_ble",
            update_interval=timedelta(seconds=DEFAULT_LOCAL_BLE_POLL_INTERVAL),
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
    if local_ble_enabled and coordinator_local_ble is not None:
        await coordinator_local_ble.async_config_entry_first_refresh()
        # PetkitLocalBleCoordinator pushes through the main coordinator's
        # async_update_listeners(); it has no entities subscribed directly.
        # HA only schedules subsequent refreshes while the listener list is
        # non-empty, so we attach a no-op listener to keep polling alive.
        # (fredrik-lindseth, PR #203 follow-up #1.)
        entry.async_on_unload(coordinator_local_ble.async_add_listener(lambda: None))

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
    hass.data[DOMAIN][COORDINATOR_LOCAL_BLE] = coordinator_local_ble

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

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    mqtt_listener = getattr(entry.runtime_data, "mqtt_listener", None)
    if mqtt_listener is not None:
        await mqtt_listener.async_stop()

    # Tear down the local BLE coordinator so toggling local BLE in options
    # takes effect without a full HA restart (fredrik-lindseth, PR #203
    # follow-up #4). Without this, the old coordinator keeps polling its
    # scheduled refresh while async_setup_entry creates a new one.
    domain_data = hass.data.get(DOMAIN, {})
    local_ble_coord = domain_data.get(COORDINATOR_LOCAL_BLE)
    if local_ble_coord is not None:
        await local_ble_coord.async_shutdown()
        domain_data[COORDINATOR_LOCAL_BLE] = None

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
        section = dict(new_options.get(NOTIFICATION_SECTION, {}))
        section.setdefault(
            CONF_ENABLED_NOTIFICATIONS, list(DEFAULT_ENABLED_NOTIFICATIONS)
        )
        new_options[NOTIFICATION_SECTION] = section
        hass.config_entries.async_update_entry(entry, options=new_options, version=8)

    if entry.version < 9:
        # PR #203: replace the legacy local_ble_enabled boolean with the new
        # connection_mode select (cloud_only / ble_only / auto). Per
        # Jezza34000's design comment (2026-05-02), the boolean's previous
        # "True" semantics map to "auto" (hybrid: BLE preferred, cloud
        # fallback) so existing setups keep working without manual edits.
        # Also seed an empty per-device BLE secret cache in entry.data; it is
        # populated lazily on the first successful CMD 86 handshake.
        new_options = dict(entry.options)
        ble_section = dict(new_options.get(LOCAL_BLE_SECTION, {}))
        if CONF_CONNECTION_MODE not in ble_section:
            legacy_enabled = ble_section.pop(CONF_LOCAL_BLE_ENABLED, False)
            ble_section[CONF_CONNECTION_MODE] = (
                MODE_AUTO if legacy_enabled else DEFAULT_CONNECTION_MODE
            )
        new_options[LOCAL_BLE_SECTION] = ble_section

        new_data = dict(entry.data)
        new_data.setdefault(CONF_BLE_SECRETS, {})

        hass.config_entries.async_update_entry(
            entry, data=new_data, options=new_options, version=9
        )

    return True


async def async_update_options(hass: HomeAssistant, entry: PetkitConfigEntry) -> None:
    """Update options."""

    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: PetkitConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    return True
