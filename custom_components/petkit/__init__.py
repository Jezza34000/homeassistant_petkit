"""Custom integration to integrate Petkit Smart Devices with Home Assistant."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

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
    FOUNTAIN_MEDIA_EVENTS,
    LOGGER,
    MEDIA_SECTION,
    NOTIFICATION_CAT_FOUNTAIN_CWT_EMPTY,
    NOTIFICATION_CAT_FOUNTAIN_CWT_LOW,
    NOTIFICATION_CAT_FOUNTAIN_WT_FULL,
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
from .schedule import (
    add_schedule_entry,
    daily_record_id_for_key,
    edit_schedule_entry,
    feed_daily_list_from_feeder,
    remove_schedule_entry,
    schedule_to_feed_daily_list,
    set_plan_weekdays,
)
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
SERVICE_ADD_FEEDING_SCHEDULE_ENTRY = "add_feeding_schedule_entry"
SERVICE_EDIT_FEEDING_SCHEDULE_ENTRY = "edit_feeding_schedule_entry"
SERVICE_REMOVE_FEEDING_SCHEDULE_ENTRY = "remove_feeding_schedule_entry"
SERVICE_SKIP_FEEDING_TODAY = "skip_feeding_today"
SERVICE_UNSKIP_FEEDING_TODAY = "unskip_feeding_today"
SERVICE_SET_FEEDING_PLAN_WEEKDAYS = "set_feeding_plan_weekdays"

FEED_ITEM_SCHEMA = vol.Schema(
    {
        vol.Required("time"): vol.All(int, vol.Range(min=0)),
        vol.Required("name"): cv.string,
        vol.Optional("amount", default=0): vol.Coerce(int),
        vol.Optional("amount1", default=0): vol.Coerce(int),
        vol.Optional("amount2", default=0): vol.Coerce(int),
        vol.Optional("id"): vol.Coerce(int),
    }
)

FEED_DAY_SCHEMA = vol.Schema(
    {
        vol.Required("repeats"): vol.Any(cv.positive_int, cv.string),
        vol.Required("items"): vol.All(cv.ensure_list, [FEED_ITEM_SCHEMA]),
        vol.Optional("suspended", default=0): vol.Coerce(int),
    }
)

SCHEDULE_ROW_SCHEMA = vol.Schema(
    {
        vol.Optional("key"): cv.string,
        vol.Required("hour"): vol.All(int, vol.Range(min=0, max=23)),
        vol.Required("minute"): vol.All(int, vol.Range(min=0, max=59)),
        vol.Required("values"): vol.All(cv.ensure_list, [vol.Coerce(int)]),
        vol.Optional("weekdays"): vol.All(
            cv.ensure_list, [vol.All(int, vol.Range(min=1, max=7))]
        ),
        vol.Optional("label"): cv.string,
    }
)

SERVICE_SET_FEEDING_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.Coerce(int),
        vol.Optional("schedule"): vol.All(cv.ensure_list, [SCHEDULE_ROW_SCHEMA]),
        vol.Optional("feed_daily_list"): vol.All(cv.ensure_list, [FEED_DAY_SCHEMA]),
    }
)

SERVICE_ADD_ENTRY_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.Coerce(int),
        vol.Required("hour"): vol.All(int, vol.Range(min=0, max=23)),
        vol.Required("minute"): vol.All(int, vol.Range(min=0, max=59)),
        vol.Required("values"): vol.All(cv.ensure_list, [vol.Coerce(int)]),
        vol.Optional("weekdays"): vol.All(
            cv.ensure_list, [vol.All(int, vol.Range(min=1, max=7))]
        ),
        vol.Optional("label"): cv.string,
    }
)

SERVICE_EDIT_ENTRY_SCHEMA = SERVICE_ADD_ENTRY_SCHEMA.extend(
    {vol.Required("key"): cv.string}
)

SERVICE_REMOVE_ENTRY_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.Coerce(int),
        vol.Required("key"): cv.string,
    }
)

SERVICE_SKIP_TODAY_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.Coerce(int),
        vol.Required("key"): cv.string,
    }
)

SERVICE_SET_PLAN_WEEKDAYS_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.Coerce(int),
        # The selector hands back strings, automations hand back ints.
        vol.Required("weekdays"): vol.All(
            cv.ensure_list,
            [vol.All(vol.Coerce(int), vol.Range(min=1, max=7))],
            vol.Length(min=1),
        ),
    }
)


def _coerce_amount(value: Any) -> int:
    """Unused hopper fields come back as None on the cloud models."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _build_feed_daily_list(feed_daily_list: list[dict]) -> list[dict]:
    """Add the computed day-level fields the app sends.

    Items are passed through with their ids and ``petAmount`` intact; the
    library normalises them onto the wire shape (real ``deviceId``, family
    ``deviceType``, and the id-from-time rewrite for newly added meals).
    """
    result = []
    for day in feed_daily_list:
        items = [dict(item) for item in day.get("items") or []]
        totals = [0, 0, 0]
        for item in items:
            for index, key in enumerate(("amount", "amount1", "amount2")):
                value = _coerce_amount(item.get(key))
                item[key] = value
                totals[index] += value
        result.append(
            {
                "count": len(items),
                "items": items,
                "repeats": str(day.get("repeats", "")),
                "suspended": _coerce_amount(day.get("suspended")),
                "totalAmount": totals[0],
                "totalAmount1": totals[1],
                "totalAmount2": totals[2],
            }
        )
    return result


def _find_feeder_client(
    hass: HomeAssistant, device_id: int
) -> tuple[PetKitClient, Feeder]:
    """Return the PetKit client and feeder entity for a numeric device id."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if hasattr(entry, "runtime_data") and entry.runtime_data:
            candidate = entry.runtime_data.client
            if device_id in candidate.petkit_entities:
                device = candidate.petkit_entities[device_id]
                if isinstance(device, Feeder):
                    return candidate, device
    raise ValueError(
        f"Feeder with device_id {device_id} not found. "
        "Ensure the device_id matches a registered Petkit feeder."
    )


async def _save_feed_days(
    client: PetKitClient, device_id: int, days: list[dict]
) -> None:
    """POST SAVE_FEED with a 7-day OEM list (library reshapes Mini)."""
    api_payload = _build_feed_daily_list(days)
    LOGGER.debug(
        "Setting feeding schedule for device %s with %d day(s)",
        device_id,
        len(api_payload),
    )
    await client.send_api_request(device_id, FeederCommand.SAVE_FEED, api_payload)


async def _async_handle_set_feeding_schedule(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the set_feeding_schedule service call."""
    device_id = call.data["device_id"]
    client, feeder = _find_feeder_client(hass, device_id)
    if call.data.get("schedule") is not None:
        previous = feed_daily_list_from_feeder(feeder)
        days = schedule_to_feed_daily_list(call.data["schedule"], feeder, previous)
        await _save_feed_days(client, device_id, days)
        return
    feed_daily_list = call.data.get("feed_daily_list")
    if not feed_daily_list:
        raise ValueError("set_feeding_schedule requires schedule or feed_daily_list")
    await _save_feed_days(client, device_id, feed_daily_list)


async def _async_handle_add_entry(hass: HomeAssistant, call: ServiceCall) -> None:
    """Merge one OpenPetBowl row then SAVE_FEED."""
    device_id = call.data["device_id"]
    client, feeder = _find_feeder_client(hass, device_id)
    days = add_schedule_entry(
        feeder,
        hour=call.data["hour"],
        minute=call.data["minute"],
        values=list(call.data["values"]),
        weekdays=call.data.get("weekdays"),
        label=call.data.get("label"),
    )
    await _save_feed_days(client, device_id, days)


async def _async_handle_edit_entry(hass: HomeAssistant, call: ServiceCall) -> None:
    """Patch one OpenPetBowl row then SAVE_FEED."""
    device_id = call.data["device_id"]
    client, feeder = _find_feeder_client(hass, device_id)
    days = edit_schedule_entry(
        feeder,
        key=call.data["key"],
        hour=call.data["hour"],
        minute=call.data["minute"],
        values=list(call.data["values"]),
        weekdays=call.data.get("weekdays"),
        label=call.data.get("label"),
    )
    await _save_feed_days(client, device_id, days)


async def _async_handle_remove_entry(hass: HomeAssistant, call: ServiceCall) -> None:
    """Drop one OpenPetBowl row then SAVE_FEED."""
    device_id = call.data["device_id"]
    client, feeder = _find_feeder_client(hass, device_id)
    days = remove_schedule_entry(feeder, call.data["key"])
    await _save_feed_days(client, device_id, days)


async def _async_handle_skip_today(
    hass: HomeAssistant, call: ServiceCall, *, restore: bool
) -> None:
    """Skip or restore today's occurrence of a plan row."""
    device_id = call.data["device_id"]
    client, feeder = _find_feeder_client(hass, device_id)
    feed_id = daily_record_id_for_key(feeder, call.data["key"])
    if not feed_id:
        raise ValueError(
            "No today's meal id for that schedule key; wait for the daily feed list."
        )
    action = (
        FeederCommand.RESTORE_DAILY_FEED if restore else FeederCommand.REMOVE_DAILY_FEED
    )
    await client.send_api_request(device_id, action, {"feed_id": feed_id})


async def _async_handle_set_plan_weekdays(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Re-save the D1/Mini plan on a new weekday mask.

    This is the app's plan-level Repeat control. It covers every meal at once —
    the family has no per-meal weekday — so it is a service rather than part of
    the schedule rows.
    """
    device_id = call.data["device_id"]
    client, feeder = _find_feeder_client(hass, device_id)
    repeats = set_plan_weekdays(feeder, list(call.data["weekdays"]))
    LOGGER.debug(
        "Setting feeding plan weekdays for device %s to %s (1=Sunday)",
        device_id,
        repeats,
    )
    await client.send_api_request(
        device_id, FeederCommand.SET_PLAN_REPEATS, {"repeats": repeats}
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

        async def handle_add_entry(call: ServiceCall) -> None:
            await _async_handle_add_entry(hass, call)

        async def handle_edit_entry(call: ServiceCall) -> None:
            await _async_handle_edit_entry(hass, call)

        async def handle_remove_entry(call: ServiceCall) -> None:
            await _async_handle_remove_entry(hass, call)

        async def handle_skip_today(call: ServiceCall) -> None:
            await _async_handle_skip_today(hass, call, restore=False)

        async def handle_unskip_today(call: ServiceCall) -> None:
            await _async_handle_skip_today(hass, call, restore=True)

        async def handle_set_plan_weekdays(call: ServiceCall) -> None:
            await _async_handle_set_plan_weekdays(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_FEEDING_SCHEDULE,
            handle_set_feeding_schedule,
            schema=SERVICE_SET_FEEDING_SCHEDULE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_ADD_FEEDING_SCHEDULE_ENTRY,
            handle_add_entry,
            schema=SERVICE_ADD_ENTRY_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_EDIT_FEEDING_SCHEDULE_ENTRY,
            handle_edit_entry,
            schema=SERVICE_EDIT_ENTRY_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_REMOVE_FEEDING_SCHEDULE_ENTRY,
            handle_remove_entry,
            schema=SERVICE_REMOVE_ENTRY_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_FEEDING_PLAN_WEEKDAYS,
            handle_set_plan_weekdays,
            schema=SERVICE_SET_PLAN_WEEKDAYS_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SKIP_FEEDING_TODAY,
            handle_skip_today,
            schema=SERVICE_SKIP_TODAY_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_UNSKIP_FEEDING_TODAY,
            handle_unskip_today,
            schema=SERVICE_SKIP_TODAY_SCHEMA,
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

    if entry.version < 9:
        new_options = dict(entry.options)
        media_section = dict(new_options.get(MEDIA_SECTION, {}))
        events = media_section.get(CONF_MEDIA_EV_TYPE)
        if events:
            events = [
                "Pet_detect" if event == "Pet_detected" else event for event in events
            ]
        else:
            events = list(DEFAULT_EVENTS)

        for event in FOUNTAIN_MEDIA_EVENTS:
            if event not in events:
                events.append(event)
        media_section[CONF_MEDIA_EV_TYPE] = events
        new_options[MEDIA_SECTION] = media_section
        section = dict(new_options.get(NOTIFICATION_SECTION, {}))
        enabled = list(
            section.get(CONF_ENABLED_NOTIFICATIONS, DEFAULT_ENABLED_NOTIFICATIONS)
        )
        for category in (
            NOTIFICATION_CAT_FOUNTAIN_CWT_EMPTY,
            NOTIFICATION_CAT_FOUNTAIN_WT_FULL,
            NOTIFICATION_CAT_FOUNTAIN_CWT_LOW,
        ):
            if category not in enabled:
                enabled.append(category)
        section[CONF_ENABLED_NOTIFICATIONS] = enabled
        new_options[NOTIFICATION_SECTION] = section
        hass.config_entries.async_update_entry(entry, options=new_options, version=9)

    return True


async def async_update_options(hass: HomeAssistant, entry: PetkitConfigEntry) -> None:
    """Update options."""

    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: PetkitConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    return True
