"""Sensor platform for Petkit Smart Devices integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pypetkitapi import (
    CTW3,
    D4H,
    D4S,
    D4SH,
    DEVICES_LITTER_BOX,
    K2,
    K3,
    LITTER_WITH_CAMERA,
    T3,
    T4,
    T5,
    T6,
    T7,
    W5,
    Feeder,
    Litter,
    Pet,
    Purifier,
    WaterFountain,
)

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfMass,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)

from .const import BATTERY_LEVEL_MAP, DEVICE_STATUS_MAP, LOGGER, NO_ERROR
from .entity import PetKitDescSensorBase, PetkitEntity
from .utils import get_raw_feed_plan, map_litter_event, map_work_state

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import (
        PetkitBluetoothUpdateCoordinator,
        PetkitDataUpdateCoordinator,
    )
    from .data import PetkitConfigEntry, PetkitDevices


@dataclass(frozen=True, kw_only=True)
class PetKitSensorDesc(PetKitDescSensorBase, SensorEntityDescription):
    """A class that describes sensor entities."""

    restore_state: bool = False
    bluetooth_coordinator: bool = False
    smart_poll_trigger: Callable[[PetkitDevices], bool] | None = None


def get_liquid_value(device):
    """Get the liquid value for purifier devices."""
    if (
        hasattr(device.state, "liquid")
        and device.state.liquid is not None
        and 0 <= device.state.liquid <= 100
    ):
        return device.state.liquid
    if (
        hasattr(device, "liquid")
        and device.liquid is not None
        and 0 <= device.liquid <= 100
    ):
        return device.liquid
    return None


COMMON_ENTITIES = [
    PetKitSensorDesc(
        key="Device status",
        translation_key="device_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda device: DEVICE_STATUS_MAP.get(device.state.pim, "Unknown Status"),
    ),
    PetKitSensorDesc(
        key="Rssi",
        translation_key="rssi",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        value=lambda device: device.state.wifi.rsq,
    ),
    PetKitSensorDesc(
        key="Error message",
        translation_key="error_message",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda device: (
            device.state.error_msg
            if hasattr(device.state, "error_msg") and device.state.error_msg is not None
            else NO_ERROR
        ),
        force_add=[K2, K3, T7],
    ),
    PetKitSensorDesc(
        key="End date care plus subscription",
        translation_key="end_date_care_plus_subscription",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.DAYS,
        value=lambda device: max(
            0,
            (
                datetime.fromtimestamp(
                    device.cloud_product.work_indate, tz=timezone.utc
                )
                - datetime.now(timezone.utc)
            ).days,
        ),
    ),
]

SENSOR_MAPPING: dict[type[PetkitDevices], list[PetKitSensorDesc]] = {
    Feeder: [
        *COMMON_ENTITIES,
        PetKitSensorDesc(
            key="Desiccant left days",
            translation_key="desiccant_left_days",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfTime.DAYS,
            value=lambda device: device.state.desiccant_left_days,
        ),
        PetKitSensorDesc(
            key="Battery level",
            translation_key="battery_level",
            entity_category=EntityCategory.DIAGNOSTIC,
            value=lambda device: (
                BATTERY_LEVEL_MAP.get(device.state.battery_status, "Unknown")
                if device.state.pim == 2
                else "Not in use"
            ),
        ),
        PetKitSensorDesc(
            key="Times dispensed",
            translation_key="times_dispensed",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.MEASUREMENT,
            value=lambda device: device.state.feed_state.times,
        ),
        PetKitSensorDesc(
            key="Total planned",
            translation_key="total_planned",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfMass.GRAMS,
            value=lambda device: device.state.feed_state.plan_amount_total,
        ),
        PetKitSensorDesc(
            key="Planned dispensed",
            translation_key="planned_dispensed",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL_INCREASING,
            native_unit_of_measurement=UnitOfMass.GRAMS,
            value=lambda device: device.state.feed_state.plan_real_amountTotal,
        ),
        PetKitSensorDesc(
            key="Total dispensed",
            translation_key="total_dispensed",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL_INCREASING,
            native_unit_of_measurement=UnitOfMass.GRAMS,
            value=lambda device: device.state.feed_state.real_amount_total,
        ),
        PetKitSensorDesc(
            key="Manual dispensed",
            translation_key="manual_dispensed",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL,
            native_unit_of_measurement=UnitOfMass.GRAMS,
            value=lambda device: device.state.feed_state.add_amount_total,
        ),
        PetKitSensorDesc(
            key="Amount eaten",
            translation_key="amount_eaten",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL,
            native_unit_of_measurement=UnitOfMass.GRAMS,
            value=lambda device: device.state.feed_state.eat_amount_total,  # D3
        ),
        PetKitSensorDesc(
            key="Times eaten",
            translation_key="times_eaten",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL,
            value=lambda device: (
                len(device.state.feed_state.eat_times)
                if device.state.feed_state.eat_times is not None
                else None
            ),
            ignore_types=[D4S],
        ),
        PetKitSensorDesc(
            key="Times eaten",
            translation_key="times_eaten",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL,
            value=lambda device: device.state.feed_state.eat_count,
            only_for_types=[D4S],
        ),
        PetKitSensorDesc(
            key="Food in bowl",
            translation_key="food_in_bowl",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfMass.GRAMS,
            value=lambda device: device.state.weight,
        ),
        PetKitSensorDesc(
            key="Avg eating time",
            translation_key="avg_eating_time",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfTime.SECONDS,
            value=lambda device: device.state.feed_state.eat_avg,
        ),
        PetKitSensorDesc(
            key="Manual dispensed hopper 1",
            translation_key="manual_dispensed_hopper_1",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL,
            value=lambda device: device.state.feed_state.add_amount_total1,
        ),
        PetKitSensorDesc(
            key="Manual dispensed hopper 2",
            translation_key="manual_dispensed_hopper_2",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL,
            value=lambda device: device.state.feed_state.add_amount_total2,
        ),
        PetKitSensorDesc(
            key="Total planned hopper 1",
            translation_key="total_planned_hopper_1",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL,
            value=lambda device: device.state.feed_state.plan_amount_total1,
        ),
        PetKitSensorDesc(
            key="Total planned hopper 2",
            translation_key="total_planned_hopper_2",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL,
            value=lambda device: device.state.feed_state.plan_amount_total2,
        ),
        PetKitSensorDesc(
            key="Planned dispensed hopper 1",
            translation_key="planned_dispensed_hopper_1",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL,
            value=lambda device: device.state.feed_state.plan_real_amount_total1,
        ),
        PetKitSensorDesc(
            key="Planned dispensed hopper 2",
            translation_key="planned_dispensed_hopper_2",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL,
            value=lambda device: device.state.feed_state.plan_real_amount_total2,
        ),
        PetKitSensorDesc(
            key="Total dispensed hopper 1",
            translation_key="total_dispensed_hopper_1",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL,
            value=lambda device: device.state.feed_state.real_amount_total1,
        ),
        PetKitSensorDesc(
            key="Total dispensed hopper 2",
            translation_key="total_dispensed_hopper_2",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL,
            value=lambda device: device.state.feed_state.real_amount_total2,
        ),
        PetKitSensorDesc(
            key="Food bowl percentage",
            translation_key="food_bowl_percentage",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PERCENTAGE,
            value=lambda device: (
                max(0, min(100, device.state.bowl))
                if device.state.bowl is not None
                else None
            ),
        ),
        PetKitSensorDesc(
            key="Food left",
            translation_key="food_left",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PERCENTAGE,
            value=lambda device: device.state.percent,
        ),
        PetKitSensorDesc(
            key="RAW distribution data",
            translation_key="raw_distribution_data",
            entity_category=EntityCategory.DIAGNOSTIC,
            value=lambda device: get_raw_feed_plan(device.device_records),
            force_add=[D4H, D4SH],
        ),
    ],
    Litter: [
        *COMMON_ENTITIES,
        PetKitSensorDesc(
            key="Litter level",
            translation_key="litter_level",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PERCENTAGE,
            value=lambda device: device.state.sand_percent,
            ignore_types=LITTER_WITH_CAMERA,
        ),
        PetKitSensorDesc(
            key="Litter weight",
            translation_key="litter_weight",
            device_class=SensorDeviceClass.WEIGHT,
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfMass.KILOGRAMS,
            value=lambda device: round((device.state.sand_weight / 1000), 1),
            ignore_types=[T7],
        ),
        PetKitSensorDesc(
            key="State",
            translation_key="litter_state",
            value=lambda device: map_work_state(device.state.work_state),
            smart_poll_trigger=lambda device: map_work_state(device.state.work_state)
            != "idle",
        ),
        PetKitSensorDesc(
            key="Litter last event",
            translation_key="litter_last_event",
            value=lambda device: map_litter_event(device.device_records),
            force_add=DEVICES_LITTER_BOX,
        ),
        PetKitSensorDesc(
            key="Odor eliminator N50 left days",
            translation_key="odor_eliminator_n50_left_days",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfTime.DAYS,
            value=lambda device: device.state.deodorant_left_days,
        ),
        PetKitSensorDesc(
            key="Odor eliminator N60 left days",
            translation_key="odor_eliminator_n60_left_days",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfTime.DAYS,
            value=lambda device: device.state.spray_left_days,
        ),
        PetKitSensorDesc(
            key="Times used T3 T4",
            translation_key="times_used",
            state_class=SensorStateClass.TOTAL,
            value=lambda device: device.device_stats.times,
            force_add=[T3, T4],
            ignore_types=[T5, T6],
        ),
        PetKitSensorDesc(
            key="Times used T5 T6",
            translation_key="times_used",
            state_class=SensorStateClass.TOTAL,
            value=lambda device: device.in_times,
            force_add=[T5, T6],
            ignore_types=[T3, T4],
        ),
        PetKitSensorDesc(
            key="Total time T3 T4",
            translation_key="total_time",
            state_class=SensorStateClass.TOTAL,
            native_unit_of_measurement=UnitOfTime.SECONDS,
            value=lambda device: device.device_stats.total_time,
            force_add=[T3, T4],
            ignore_types=[T5, T6],
        ),
        PetKitSensorDesc(
            key="Total time T5 T6",
            translation_key="total_time",
            state_class=SensorStateClass.TOTAL,
            native_unit_of_measurement=UnitOfTime.SECONDS,
            value=lambda device: device.total_time,
            force_add=[T5, T6],
            ignore_types=[T3, T4],
        ),
        PetKitSensorDesc(
            key="Average time",
            translation_key="average_time",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfTime.SECONDS,
            value=lambda device: device.device_stats.avg_time,
        ),
        PetKitSensorDesc(
            key="Last used by",
            translation_key="last_used_by",
            value=lambda device: (
                device.device_stats.statistic_info[-1].pet_name
                if device.device_stats.statistic_info
                else None
            ),
            force_add=[T3, T4],
            restore_state=True,
        ),
        PetKitSensorDesc(
            key="Last used by",
            translation_key="last_used_by",
            value=lambda device: (
                device.device_pet_graph_out[-1].pet_name
                if device.device_pet_graph_out
                else None
            ),
            force_add=LITTER_WITH_CAMERA,
            restore_state=True,
        ),
        PetKitSensorDesc(
            key="Total package",
            translation_key="total_package",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL,
            value=lambda device: device.package_total_count,
        ),
        PetKitSensorDesc(
            key="Package used",
            translation_key="package_used",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.MEASUREMENT,
            value=lambda device: device.package_used_count,
        ),
    ],
    WaterFountain: [
        *COMMON_ENTITIES,
        PetKitSensorDesc(
            key="Today pump run time",
            translation_key="today_pump_run_time",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.ENERGY,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            value=lambda device: round(
                ((0.75 * int(device.today_pump_run_time)) / 3600000), 4
            ),
        ),
        PetKitSensorDesc(
            key="Last update",
            translation_key="last_update",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.TIMESTAMP,
            value=lambda device: datetime.fromisoformat(
                device.update_at.replace(".000Z", "+00:00")
            ),
        ),
        PetKitSensorDesc(
            key="Filter percent",
            translation_key="filter_percent",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PERCENTAGE,
            value=lambda device: device.filter_percent,
        ),
        PetKitSensorDesc(
            key="Purified water",
            translation_key="purified_water",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.MEASUREMENT,
            value=lambda device: int(
                ((1.5 * int(device.today_pump_run_time)) / 60) / 3.0
            ),
            only_for_types=[CTW3],
        ),
        PetKitSensorDesc(
            key="Purified water",
            translation_key="purified_water",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.MEASUREMENT,
            value=lambda device: int(
                ((1.5 * int(device.today_pump_run_time)) / 60) / 2.0
            ),
            ignore_types=[CTW3],
        ),
        PetKitSensorDesc(
            key="Drink times",
            translation_key="drink_times",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.TOTAL,
            value=lambda device: (
                len(device.device_records)
                if isinstance(device.device_records, list)
                else None
            ),
        ),
        PetKitSensorDesc(
            key="Battery",
            translation_key="battery",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.BATTERY,
            native_unit_of_measurement=PERCENTAGE,
            value=lambda device: device.electricity.battery_percent,
        ),
        PetKitSensorDesc(
            key="Battery voltage",
            translation_key="battery_voltage",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.VOLTAGE,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            value=lambda device: (
                round(device.electricity.battery_voltage / 1000, 1)
                if isinstance(device.electricity.battery_voltage, (int, float))
                and device.electricity.battery_voltage > 0
                else None
            ),
        ),
        PetKitSensorDesc(
            key="Supply voltage",
            translation_key="supply_voltage",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.VOLTAGE,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            value=lambda device: (
                round(device.electricity.supply_voltage / 1000, 1)
                if isinstance(device.electricity.supply_voltage, (int, float))
                and device.electricity.supply_voltage > 0
                else None
            ),
        ),
    ],
    Purifier: [
        *COMMON_ENTITIES,
        PetKitSensorDesc(
            key="Humidity",
            translation_key="humidity",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PERCENTAGE,
            device_class=SensorDeviceClass.HUMIDITY,
            value=lambda device: round(device.state.humidity / 10),
        ),
        PetKitSensorDesc(
            key="Temperature",
            translation_key="temperature",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
            value=lambda device: round(device.state.temp / 10),
        ),
        PetKitSensorDesc(
            key="Air purified",
            translation_key="air_purified",
            state_class=SensorStateClass.TOTAL,
            native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            device_class=SensorDeviceClass.VOLUME,
            value=lambda device: round(device.state.refresh),
        ),
        PetKitSensorDesc(
            key="Purifier liquid",
            translation_key="purifier_liquid",
            entity_category=EntityCategory.DIAGNOSTIC,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PERCENTAGE,
            value=get_liquid_value,
        ),
        PetKitSensorDesc(
            key="Battery",
            translation_key="battery",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.BATTERY,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PERCENTAGE,
            value=lambda device: device.battery,
        ),
        PetKitSensorDesc(
            key="Battery voltage",
            translation_key="battery_voltage",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.VOLTAGE,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            value=lambda device: (
                round(device.voltage / 1000, 1)
                if isinstance(device.voltage, (int, float)) and device.voltage > 0
                else None
            ),
        ),
        # PetKitSensorDesc(
        #     key="Spray times",
        #     translation_key="spray_times",
        #     state_class=SensorStateClass.TOTAL,
        #     value=lambda device: device.spray_times,
        # ),
    ],
    Pet: [
        PetKitSensorDesc(
            key="Pet last weight measurement",
            translation_key="pet_last_weight_measurement",
            entity_picture=lambda pet: pet.avatar,
            device_class=SensorDeviceClass.WEIGHT,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfMass.KILOGRAMS,
            value=lambda pet: (
                round((pet.last_measured_weight / 1000), 2)
                if pet.last_measured_weight is not None
                else None
            ),
            restore_state=True,
        ),
        PetKitSensorDesc(
            key="Pet last use duration",
            translation_key="pet_last_use_duration",
            entity_picture=lambda pet: pet.avatar,
            device_class=SensorDeviceClass.DURATION,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfTime.SECONDS,
            value=lambda pet: pet.last_duration_usage,
            restore_state=True,
        ),
        PetKitSensorDesc(
            key="Pet last device used",
            translation_key="pet_last_device_used",
            entity_picture=lambda pet: pet.avatar,
            value=lambda pet: pet.last_device_used,
            restore_state=True,
        ),
        PetKitSensorDesc(
            key="Pet last use date",
            translation_key="pet_last_use_date",
            entity_picture=lambda pet: pet.avatar,
            value=lambda pet: (
                datetime.fromtimestamp(pet.last_litter_usage)
                if pet.last_litter_usage is not None and pet.last_litter_usage != 0
                else "Unknown"
            ),
            restore_state=True,
        ),
        PetKitSensorDesc(
            key="Urine measured ph",
            translation_key="urine_measured_ph",
            entity_picture=lambda pet: pet.avatar,
            state_class=SensorStateClass.MEASUREMENT,
            value=lambda pet: pet.measured_ph,
            restore_state=True,
        ),
        PetKitSensorDesc(
            key="Pet last urination date",
            translation_key="pet_last_urination_date",
            entity_picture=lambda pet: pet.avatar,
            value=lambda pet: (
                None
                if pet.last_urination is None
                else (
                    "Unknown"
                    if pet.last_urination == 0
                    else datetime.fromtimestamp(pet.last_urination)
                )
            ),
            restore_state=True,
        ),
        PetKitSensorDesc(
            key="Pet last defecation date",
            translation_key="pet_last_defecation_date",
            entity_picture=lambda pet: pet.avatar,
            value=lambda pet: (
                None
                if pet.last_defecation is None
                else (
                    "Unknown"
                    if pet.last_defecation == 0
                    else datetime.fromtimestamp(pet.last_defecation)
                )
            ),
            restore_state=True,
        ),
    ],
}

SENSOR_BT_MAPPING: dict[type[PetkitDevices], list[PetKitSensorDesc]] = {
    WaterFountain: [
        PetKitSensorDesc(
            key="Last connection",
            translation_key="last_connection",
            entity_category=EntityCategory.DIAGNOSTIC,
            device_class=SensorDeviceClass.TIMESTAMP,
            value=lambda device: (
                device.coordinator_bluetooth.last_update_timestamps.get(device.id)
                if hasattr(device, "coordinator_bluetooth")
                and device.coordinator_bluetooth.last_update_timestamps.get(device.id)
                else None
            ),
            bluetooth_coordinator=True,
            force_add=[CTW3, W5],
        )
    ]
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary_sensors using config entry."""
    devices = entry.runtime_data.client.petkit_entities.values()
    entities = [
        PetkitSensor(
            coordinator=entry.runtime_data.coordinator,
            entity_description=entity_description,
            device=device,
        )
        for device in devices
        for device_type, entity_descriptions in SENSOR_MAPPING.items()
        if isinstance(device, device_type)
        for entity_description in entity_descriptions
        if entity_description.is_supported(device)  # Check if the entity is supported
    ]
    LOGGER.debug(
        "SENSOR : Adding %s (on %s available)",
        len(entities),
        len(SENSOR_MAPPING.items()),
    )
    entities_bt = [
        PetkitSensorBt(
            coordinator_bluetooth=entry.runtime_data.coordinator_bluetooth,
            entity_description=entity_description,
            device=device,
        )
        for device in devices
        for device_type, entity_descriptions in SENSOR_BT_MAPPING.items()
        if isinstance(device, device_type)
        for entity_description in entity_descriptions
        if entity_description.is_supported(device)  # Check if the entity is supported
    ]
    LOGGER.debug(
        "SENSOR BT : Adding %s (on %s available)",
        len(entities_bt),
        sum(len(descriptors) for descriptors in SENSOR_MAPPING.values()),
    )
    async_add_entities(entities + entities_bt)


class PetkitSensor(PetkitEntity, SensorEntity):
    """Petkit Smart Devices BinarySensor class."""

    entity_description: PetKitSensorDesc

    def __init__(
        self,
        coordinator: PetkitDataUpdateCoordinator,
        entity_description: PetKitSensorDesc,
        device: PetkitDevices,
    ) -> None:
        """Initialize the binary_sensor class."""
        super().__init__(coordinator, device)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self.device = device

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        device_data = self.coordinator.data.get(self.device.id)
        if device_data:
            return self.entity_description.value(device_data)
        return None

    @property
    def entity_picture(self) -> str | None:
        """Grab associated pet picture."""
        if self.entity_description.entity_picture:
            return self.entity_description.entity_picture(self.device)
        return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit of measurement."""
        return self.entity_description.native_unit_of_measurement

    def check_smart_poll_trigger(self) -> bool:
        """Check if fast poll trigger condition is met."""
        if self.entity_description.smart_poll_trigger:
            return self.entity_description.smart_poll_trigger(self.device)
        return False


class PetkitSensorBt(PetkitEntity, SensorEntity):
    """Petkit Smart Devices Bluetooth Sensor class."""

    entity_description: PetKitSensorDesc

    def __init__(
        self,
        coordinator_bluetooth: PetkitBluetoothUpdateCoordinator,
        entity_description: PetKitSensorDesc,
        device: PetkitDevices,
    ) -> None:
        """Initialize the Bluetooth sensor class."""
        super().__init__(coordinator_bluetooth, device)
        self.coordinator_bluetooth = coordinator_bluetooth
        self.entity_description = entity_description
        self.device = device

    @property
    def native_value(self) -> Any:
        """Return the state of the Bluetooth sensor."""
        device_data = self.coordinator_bluetooth.data.get(self.device.id)
        if device_data:
            return device_data
        return None

    @property
    def unique_id(self) -> str:
        """Return a unique ID for the Bluetooth sensor."""
        return f"{self.device.device_nfo.device_type}_{self.device.sn}_{self.entity_description.key}"

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit of measurement."""
        return self.entity_description.native_unit_of_measurement
