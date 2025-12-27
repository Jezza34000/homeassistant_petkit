"""Switch platform for Petkit Smart Devices integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pypetkitapi import (
    DEVICES_LITTER_BOX,
    FEEDER_MINI,
    LITTER_WITH_CAMERA,
    T7,
    DeviceAction,
    DeviceCommand,
    Feeder,
    Litter,
    Pet,
    Purifier,
    WaterFountain,
)

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory

from .const import LOGGER, MIN_SCAN_INTERVAL, POWER_ONLINE_STATE
from .entity import PetKitDescSensorBase, PetkitEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import PetkitDataUpdateCoordinator
    from .data import PetkitConfigEntry, PetkitDevices


@dataclass(frozen=True, kw_only=True)
class PetKitSwitchDesc(PetKitDescSensorBase, SwitchEntityDescription):
    """A class that describes sensor entities."""

    turn_on: Callable[[Any, Any], Any] | None = None
    turn_off: Callable[[Any, Any], Any] | None = None
    set_value: Callable[[Any, Any, Any], Any] | None = None


COMMON_ENTITIES = [
    PetKitSwitchDesc(
        key="Indicator light",
        translation_key="indicator_light",
        value=lambda device: device.settings.light_mode,
        entity_category=EntityCategory.CONFIG,
        turn_on=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"lightMode": 1}
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"lightMode": 0}
        ),
        ignore_types=[*DEVICES_LITTER_BOX, FEEDER_MINI],
    ),
    PetKitSwitchDesc(
        key="Display",
        translation_key="display",
        value=lambda device: device.settings.light_mode,
        entity_category=EntityCategory.CONFIG,
        turn_on=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"lightMode": 1}
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"lightMode": 0}
        ),
        only_for_types=DEVICES_LITTER_BOX,
    ),
    PetKitSwitchDesc(
        key="Indicator light",
        translation_key="indicator_light",
        value=lambda device: device.settings.light_mode,
        entity_category=EntityCategory.CONFIG,
        turn_on=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"settings.lightMode": 1}
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"settings.lightMode": 0}
        ),
        only_for_types=[FEEDER_MINI],
    ),
    PetKitSwitchDesc(
        key="Child lock",
        translation_key="child_lock",
        value=lambda device: device.settings.manual_lock,
        entity_category=EntityCategory.CONFIG,
        turn_on=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"manualLock": 1}
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"manualLock": 0}
        ),
        ignore_types=[FEEDER_MINI],
    ),
    PetKitSwitchDesc(
        key="Child lock",
        translation_key="child_lock",
        value=lambda device: device.settings.manual_lock,
        entity_category=EntityCategory.CONFIG,
        turn_on=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"settings.manualLock": 1}
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"settings.manualLock": 0}
        ),
        only_for_types=[FEEDER_MINI],
    ),
    PetKitSwitchDesc(
        key="Camera",
        translation_key="camera",
        value=lambda device: device.settings.camera,
        turn_on=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"camera": 1}
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"camera": 0}
        ),
    ),
    PetKitSwitchDesc(
        key="Do not disturb",
        translation_key="do_not_disturb",
        value=lambda device: device.settings.disturb_mode,
        entity_category=EntityCategory.CONFIG,
        turn_on=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"disturbMode": 1}
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"disturbMode": 0}
        ),
    ),
    PetKitSwitchDesc(
        key="Pet tracking",
        translation_key="pet_tracking",
        value=lambda device: device.settings.highlight,
        entity_category=EntityCategory.CONFIG,
        turn_on=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"highlight": 1}
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"highlight": 0}
        ),
        ignore_types=LITTER_WITH_CAMERA,
    ),
    PetKitSwitchDesc(
        key="Video timestamp",
        translation_key="video_timestamp",
        value=lambda device: device.settings.time_display,
        entity_category=EntityCategory.CONFIG,
        turn_on=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"timeDisplay": 1}
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"timeDisplay": 0}
        ),
    ),
    PetKitSwitchDesc(
        key="Microphone",
        translation_key="microphone",
        value=lambda device: device.settings.microphone,
        entity_category=EntityCategory.CONFIG,
        turn_on=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"microphone": 1}
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"microphone": 0}
        ),
    ),
    PetKitSwitchDesc(
        key="Night vision",
        translation_key="night_vision",
        value=lambda device: device.settings.night,
        entity_category=EntityCategory.CONFIG,
        turn_on=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"night": 1}
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"night": 0}
        ),
        ignore_types=[T7],
    ),
    PetKitSwitchDesc(
        key="Lack Liquid Notify",
        translation_key="lack_liquid_notify",
        value=lambda device: device.settings.lack_liquid_notify,
        entity_category=EntityCategory.CONFIG,
        turn_on=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"lackLiquidNotify": 1}
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"lackLiquidNotify": 0}
        ),
    ),
    PetKitSwitchDesc(
        key="System notification",
        translation_key="system_notification",
        value=lambda device: device.settings.system_sound_enable,
        entity_category=EntityCategory.CONFIG,
        turn_on=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"systemSoundEnable": 1}
        ),
        turn_off=lambda api, device: api.send_api_request(
            device.id, DeviceCommand.UPDATE_SETTING, {"systemSoundEnable": 0}
        ),
    ),
]

SWITCH_MAPPING: dict[type[PetkitDevices], list[PetKitSwitchDesc]] = {
    Feeder: [
        *COMMON_ENTITIES,
        PetKitSwitchDesc(
            key="Shortage alarm",
            translation_key="shortage_alarm",
            value=lambda device: device.settings.food_warn,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"foodWarn": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"foodWarn": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Feed tone",
            translation_key="feed_tone",
            value=lambda device: device.settings.feed_tone,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"feedTone": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"feedTone": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Feed sound",
            translation_key="feed_sound",
            value=lambda device: device.settings.feed_sound,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"feedSound": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"feedSound": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Dispensing notif",
            translation_key="dispensing_notif",
            value=lambda device: device.settings.feed_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"feedNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"feedNotify": 0}
            ),
            ignore_types=FEEDER_MINI,
        ),
        PetKitSwitchDesc(
            key="Dispensing notif",
            translation_key="dispensing_notif",
            value=lambda device: device.settings.feed_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"settings.feedNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"settings.feedNotify": 0}
            ),
            only_for_types=FEEDER_MINI,
        ),
        PetKitSwitchDesc(
            key="Refill notif",
            translation_key="refill_notif",
            value=lambda device: device.settings.food_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"foodNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"foodNotify": 0}
            ),
            ignore_types=[FEEDER_MINI],
        ),
        PetKitSwitchDesc(
            key="Refill notif",
            translation_key="refill_notif",
            value=lambda device: device.settings.food_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"settings.foodNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"settings.foodNotify": 0}
            ),
            only_for_types=[FEEDER_MINI],
        ),
        PetKitSwitchDesc(
            key="Pet visit notif",
            translation_key="pet_visit_notif",
            value=lambda device: device.settings.pet_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"petNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"petNotify": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Pet eat notif",
            translation_key="pet_eat_notif",
            value=lambda device: device.settings.eat_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"eatNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"eatNotify": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Move notif",
            translation_key="move_notif",
            value=lambda device: device.settings.move_detection,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"moveNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"moveNotify": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Surplus control",
            translation_key="surplus_control",
            value=lambda device: device.settings.surplus_control,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"surplusControl": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"surplusControl": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Low battery notif",
            translation_key="low_battery_notif",
            value=lambda device: device.settings.low_battery_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"lowBatteryNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"lowBatteryNotify": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Voice dispense",
            translation_key="voice_dispense",
            value=lambda device: device.settings.sound_enable,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"soundEnable": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"soundEnable": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Desiccant notif",
            translation_key="desiccant_notif",
            value=lambda device: device.settings.desiccant_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"desiccantNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"desiccantNotify": 0}
            ),
            ignore_types=[FEEDER_MINI],
        ),
        PetKitSwitchDesc(
            key="Desiccant notif",
            translation_key="desiccant_notif",
            value=lambda device: device.settings.desiccant_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"settings.desiccantNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"settings.desiccantNotify": 0}
            ),
            only_for_types=[FEEDER_MINI],
        ),
    ],
    Litter: [
        *COMMON_ENTITIES,
        PetKitSwitchDesc(
            # For T3/T4 only
            key="Auto deodorizing",
            translation_key="auto_deodor",
            value=lambda device: device.settings.auto_refresh,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"autoRefresh": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"autoRefresh": 0}
            ),
        ),
        PetKitSwitchDesc(
            # For T5/T6 only
            key="Auto deodorizing",
            translation_key="auto_deodor",
            value=lambda device: device.settings.auto_spray,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"autoSpray": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"autoSpray": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Auto clean",
            translation_key="auto_clean",
            value=lambda device: device.settings.auto_work,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"autoWork": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"autoWork": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Avoid repeat clean",
            translation_key="avoid_repeat_clean",
            value=lambda device: device.settings.avoid_repeat,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"avoidRepeat": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"avoidRepeat": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Periodic cleaning",
            translation_key="periodic_cleaning",
            value=lambda device: device.settings.fixed_time_clear,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"fixedTimeClear": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"fixedTimeClear": 0}
            ),
        ),
        PetKitSwitchDesc(
            # For T3/T4 only
            key="Periodic deodorizing",
            translation_key="periodic_deodorizing",
            value=lambda device: device.settings.fixed_time_refresh,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"fixedTimeRefresh": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"fixedTimeRefresh": 0}
            ),
        ),
        PetKitSwitchDesc(
            # For T5/T6 only
            key="Periodic deodorizing",
            translation_key="periodic_deodorizing",
            value=lambda device: device.settings.fixed_time_spray,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"fixedTimeSpray": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"fixedTimeSpray": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Kitten mode",
            translation_key="kitten_mode",
            value=lambda device: device.settings.kitten,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"kitten": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"kitten": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Light weight",
            translation_key="light_weight",
            value=lambda device: device.settings.underweight,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"underweight": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"underweight": 0}
            ),
            ignore_types=LITTER_WITH_CAMERA,
        ),
        PetKitSwitchDesc(
            key="Power",
            translation_key="power",
            value=lambda device: device.state.power,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.CONTROL_DEVICE, {DeviceAction.POWER: 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.CONTROL_DEVICE, {DeviceAction.POWER: 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Cont rotation",
            translation_key="cont_rotation",
            value=lambda device: device.settings.downpos,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"downpos": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"downpos": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Deep cleaning",
            translation_key="deep_cleaning",
            value=lambda device: device.settings.deep_clean,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"deepClean": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"deepClean": 0}
            ),
            ignore_types=[T7],
        ),
        PetKitSwitchDesc(
            # For T3/T4 only
            key="Deep deodorizing T3 T4",
            translation_key="deep_deodor",
            value=lambda device: device.settings.deep_refresh,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"deepRefresh": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"deepRefresh": 0}
            ),
        ),
        PetKitSwitchDesc(
            # For T5/T6 only
            key="Deep deodorizing T5 T6",
            translation_key="deep_deodor",
            value=lambda device: device.settings.deep_spray,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"deepSpray": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"deepSpray": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Sand Saving",
            translation_key="sand_saving",
            value=lambda device: device.settings.sand_saving,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"sandSaving": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"sandSaving": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Pet visit notif",
            translation_key="pet_visit_notif",
            value=lambda device: device.settings.pet_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"petNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"petNotify": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Waste covering",
            translation_key="waste_covering",
            value=lambda device: device.settings.bury,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"bury": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"bury": 0}
            ),
            ignore_types=[T7],
        ),
        PetKitSwitchDesc(
            key="Litter full notify",
            translation_key="litter_full_notify",
            value=lambda device: device.settings.litter_full_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"litterFullNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"litterFullNotify": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Pet in notify",
            translation_key="pet_in_notify",
            value=lambda device: device.settings.pet_in_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"petInNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"petInNotify": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Work notify",
            translation_key="work_notify",
            value=lambda device: device.settings.work_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"workNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"workNotify": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Deodorant N50 notify",
            translation_key="deodorant_n50_notify",
            value=lambda device: device.settings.deodorant_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"deodorantNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"deodorantNotify": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Deodorant N60 notify",
            translation_key="deodorant_n60_notify",
            value=lambda device: device.settings.spray_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"sprayNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"sprayNotify": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Lack sand notify",
            translation_key="lack_sand_notify",
            value=lambda device: device.settings.lack_sand_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"lackSandNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"lackSandNotify": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Work log notify",
            translation_key="work_log_notify",
            value=lambda device: device.settings.log_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"logNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"logNotify": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Light Assist",
            translation_key="light_assist",
            value=lambda device: device.settings.light_assist,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"lightAssist": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"lightAssist": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Camera Light",
            translation_key="camera_light",
            value=lambda device: device.settings.camera_light,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"cameraLight": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"cameraLight": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Notif pet toileting",
            translation_key="pet_toileting_notif",
            value=lambda device: device.settings.toilet_notify,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"toiletNotify": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"toiletNotify": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Toilet light",
            translation_key="toilet_light",
            value=lambda device: device.settings.toilet_light,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"toiletLight": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"toiletLight": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Privacy mode",
            translation_key="privacy_mode",
            value=lambda device: device.settings.home_mode,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"homeMode": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"homeMode": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Privacy Camera OFF",
            translation_key="privacy_camera_off",
            value=lambda device: device.settings.camera_off,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"cameraOff": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"cameraOff": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Privacy Camera inward",
            translation_key="privacy_camera_inward",
            value=lambda device: device.settings.camera_inward,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"cameraInward": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"cameraInward": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Privacy Microphone OFF",
            translation_key="privacy_microphone_off",
            value=lambda device: device.settings.no_sound,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"noSound": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"noSound": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="AI Urinary PhDetection",
            translation_key="ai_ph_detection",
            value=lambda device: device.settings.ph_detection,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"phDetection": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"phDetection": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="AI Yowling detection",
            translation_key="ai_yowling",
            value=lambda device: device.settings.voice,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"voice": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"voice": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="AI soft stool detection",
            translation_key="soft_stool",
            value=lambda device: device.settings.soft_mode,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"softMode": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"softMode": 0}
            ),
        ),
        PetKitSwitchDesc(
            key="Soft stool stop cleaning",
            translation_key="soft_stool_stop_cleaning",
            value=lambda device: device.settings.soft_mode_clean,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"softModeClean": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"softModeClean": 0}
            ),
        ),
    ],
    WaterFountain: [*COMMON_ENTITIES],
    Purifier: [
        *COMMON_ENTITIES,
        PetKitSwitchDesc(
            key="System notification",
            translation_key="system_notification",
            value=lambda device: device.settings.sound,
            entity_category=EntityCategory.CONFIG,
            turn_on=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"sound": 1}
            ),
            turn_off=lambda api, device: api.send_api_request(
                device.id, DeviceCommand.UPDATE_SETTING, {"sound": 0}
            ),
        ),
    ],
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
        PetkitSwitch(
            coordinator=entry.runtime_data.coordinator,
            entity_description=entity_description,
            device=device,
        )
        for device in devices
        for device_type, entity_descriptions in SWITCH_MAPPING.items()
        if isinstance(device, device_type)
        for entity_description in entity_descriptions
        if entity_description.is_supported(device)  # Check if the entity is supported
    ]
    LOGGER.debug(
        "SWITCH : Adding %s (on %s available)",
        len(entities),
        sum(len(descriptors) for descriptors in SWITCH_MAPPING.values()),
    )
    async_add_entities(entities)


class PetkitSwitch(PetkitEntity, SwitchEntity):
    """Petkit Smart Devices Switch class."""

    entity_description: PetKitSwitchDesc

    def __init__(
        self,
        coordinator: PetkitDataUpdateCoordinator,
        entity_description: PetKitSwitchDesc,
        device: Feeder | Litter | WaterFountain,
    ) -> None:
        """Initialize the switch class."""
        super().__init__(coordinator, device)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self.device = device

    @property
    def available(self) -> bool:
        """Return if this button is available or not."""
        device_data = self.coordinator.data.get(self.device.id)
        if hasattr(device_data.state, "pim"):
            return device_data.state.pim in POWER_ONLINE_STATE
        return True

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        updated_device = self.coordinator.data.get(self.device.id)
        if updated_device and self.entity_description.value:
            return bool(self.entity_description.value(updated_device))
        return None

    async def async_turn_on(self, **_: Any) -> None:
        """Turn on the switch."""
        LOGGER.debug("Turn ON")
        res = await self.entity_description.turn_on(
            self.coordinator.config_entry.runtime_data.client, self.device
        )
        await self._update_coordinator_data(res)

    async def async_turn_off(self, **_: Any) -> None:
        """Turn off the switch."""
        LOGGER.debug("Turn OFF")
        res = await self.entity_description.turn_off(
            self.coordinator.config_entry.runtime_data.client, self.device
        )
        await self._update_coordinator_data(res)

    async def _update_coordinator_data(self, result: bool) -> None:
        """Update the coordinator data based on the result."""
        self.coordinator.update_interval = timedelta(seconds=MIN_SCAN_INTERVAL)
        self.coordinator.fast_poll_tic = 3
        await asyncio.sleep(1)
        await self.coordinator.async_request_refresh()
