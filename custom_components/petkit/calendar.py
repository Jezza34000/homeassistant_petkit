"""Calendar platform for Petkit Smart Devices integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import random
from typing import TYPE_CHECKING, Any

from pypetkitapi.const import D4SH
from pypetkitapi.containers import Pet
from pypetkitapi.feeder_container import Feeder
from pypetkitapi.litter_container import Litter
from pypetkitapi.water_fountain_container import WaterFountain

from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityDescription,
    CalendarEvent,
)

from .const import CAT_EAT_EMOJI, CAT_VISIT_EMOJI, DISPENSE_EMOJI, DOMAIN
from .entity import PetKitDescSensorBase, PetkitEntity
from .utils import get_dispense_status

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import PetkitDataUpdateCoordinator
    from .data import PetkitConfigEntry


@dataclass(frozen=True, kw_only=True)
class PetkitCalendarDesc(PetKitDescSensorBase, CalendarEntityDescription):
    """A class that describes sensor entities."""


CALENDAR_MAPPING: dict[
    type[Feeder | Litter | WaterFountain], list[PetkitCalendarDesc]
] = {
    Feeder: [
        PetkitCalendarDesc(
            key="Feeder Event",
        ),
    ],
    Litter: [],
    WaterFountain: [],
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up calendar entities using config entry."""
    devices = entry.runtime_data.client.petkit_entities.values()
    entities = []

    for device in devices:
        for device_type, entity_descriptions in CALENDAR_MAPPING.items():
            if isinstance(device, device_type):
                for entity_description in entity_descriptions:
                    if entity_description.is_supported(device):
                        entities.append(
                            PetkitCalendar(
                                coordinator=entry.runtime_data.coordinator,
                                entity_description=entity_description,
                                device=device,
                            )
                        )

    print(f">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> Entities count : {len(entities)}")
    async_add_entities(entities)


class PetkitCalendar(PetkitEntity, CalendarEntity):
    """Representation of a Petkit Feeder calendar entity."""

    entity_description: PetkitCalendarDesc

    def __init__(
        self,
        coordinator: PetkitDataUpdateCoordinator,
        entity_description: PetkitCalendarDesc,
        device: Feeder | Litter | WaterFountain | Pet,
    ) -> None:
        """Initialize the switch class."""
        super().__init__(coordinator, device)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self.device = device
        self._event_list: list[CalendarEvent] = []

    @property
    def unique_id(self) -> str:
        """Return a unique ID for the binary_sensor."""
        return (
            f"{self.device.device_type}_{self.device.sn}_{self.entity_description.key}"
        )

    @property
    def event(self) -> CalendarEvent | None:
        """Return the current active event if any."""
        return self._event_list[0] if self._event_list else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Get all events in a specific time range."""
        self._event_list.clear()

        if isinstance(self.device.device_records.pet, list):
            for record in self.device.device_records.pet[0].items:
                if self.device.device_type != D4SH:
                    continue

                start_date = datetime.fromtimestamp(record.timestamp, tz=timezone.utc)
                end_date = start_date + timedelta(minutes=1)

                cal_emoji = random.choice(CAT_VISIT_EMOJI)

                self._event_list.append(
                    CalendarEvent(
                        start=start_date,
                        end=end_date,
                        summary=f"{cal_emoji} has come to visit",
                        description="Pet was here to visit",
                        location="France",
                        uid=f"{DOMAIN}_{self.device.sn}_visit_event_{record.timestamp}",
                    )
                )

        if isinstance(self.device.device_records.eat, list):
            for record in self.device.device_records.eat[0].items:
                if self.device.device_type != D4SH:
                    continue
                start_timestamp = record.eat_start_time
                end_timestamp = record.eat_end_time

                start_date = datetime.fromtimestamp(start_timestamp, tz=timezone.utc)
                end_date = datetime.fromtimestamp(end_timestamp, tz=timezone.utc)

                cal_emoji = random.choice(CAT_EAT_EMOJI)

                self._event_list.append(
                    CalendarEvent(
                        start=start_date,
                        end=end_date,
                        summary=f"{cal_emoji} is eating",
                        description="Pet has come to eat those delicious treats",
                        location="France",
                        uid=f"{DOMAIN}_{self.device.sn}_eat_event_{record.eat_start_time}",
                    )
                )

        if isinstance(self.device.device_records.feed, list):
            for record in self.device.device_records.feed[0].items:
                self._event_list.append(self._convert_record_to_calendar_event(record))
        return self._event_list

    @staticmethod
    def _convert_record_to_calendar_event(record: Any) -> CalendarEvent:
        """Convert the record to a calendar event."""

        # Start/End time conversion
        start_time = record.time
        start_date = datetime.combine(
            datetime.today(), datetime.min.time(), tzinfo=timezone.utc
        ) + timedelta(seconds=start_time)
        end_date = start_date + timedelta(minutes=1)

        source, status, plan_amount1, plan_amount2, disp_amount1, disp_amount2 = (
            get_dispense_status(record)
        )

        emoji_status = DISPENSE_EMOJI.get(status, "❓")

        summary = f"{emoji_status} {status.capitalize()} - {source.capitalize()}"
        description = (
            f"Plan amount 1: {plan_amount1}Plan amount 2: {plan_amount2}\r\nDispense amount 1: "
            f"{disp_amount1}\r\nDispense amount 2: {disp_amount2}"
        )

        return CalendarEvent(
            start=start_date,
            end=end_date,
            summary=summary,
            description=description,
            location="France",
            uid=f"{DOMAIN}_eat_event_{record.id}",
        )
