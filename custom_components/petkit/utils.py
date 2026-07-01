"""Util functions for the Petkit integration."""

from datetime import datetime
import json

from pypetkitapi import LitterRecord, RecordsItems, WorkState

from .const import EVENT_MAPPING, LOGGER


def map_work_state(work_state: WorkState | None) -> str:
    """Get the state of the litter box.

    Use the 'litter_state' translation table to map the state to a human-readable string.
    """

    LOGGER.debug("Litter map work_state: %s", work_state)

    if work_state is None:
        return "idle"

    def get_safe_warn_status(safe_warn: int, pet_in_time: int) -> str:
        """Get the safe warn status."""
        if safe_warn != 0:
            return {
                1: "pet_entered",
                3: "cover",
            }.get(safe_warn, "system_error")
        return "pet_approach" if pet_in_time == 0 else "pet_using"

    def handle_process_mapping(prefix: str) -> str:
        """Handle the process mapping."""
        major, minor = divmod(work_state.work_process, 10)

        if major == 1:
            return f"{prefix}"
        if major == 2:
            if minor == 2:
                return f"{prefix}_paused_{get_safe_warn_status(work_state.safe_warn, work_state.pet_in_time)}"
            return f"{prefix}_paused"
        if major == 3:
            return "resetting_device"
        if major == 4:
            if minor == 2:
                return f"paused_{get_safe_warn_status(work_state.safe_warn, work_state.pet_in_time)}"
            return "paused"
        return f"{prefix}"

    # Map work_mode to their respective functions
    _WORK_MODE_MAPPING = {
        0: lambda: handle_process_mapping("cleaning"),
        1: lambda: handle_process_mapping("dumping"),
        2: lambda: "odor_removal",
        3: lambda: "resetting",
        4: lambda: "leveling",
        5: lambda: "calibrating",
        6: lambda: "reset_deodorant",
        7: lambda: "light",
        8: lambda: "reset_max_deodorant",
        9: lambda: handle_process_mapping("maintenance"),
    }

    return _WORK_MODE_MAPPING.get(work_state.work_mode, lambda: "idle")()


def get_raw_schedule(feeder) -> dict[str, any] | None:
    """Get the full 7-day feeding schedule with per-hopper amounts.

    Returns a dict suitable for extra_state_attributes containing:
      - device_id: int
      - feed_daily_list: list of day objects, each with items preserving
        amount1/amount2 for dual-hopper round-trip editing.
    """
    multi = getattr(feeder, "multi_feed_item", None)
    if multi is None or multi.feed_daily_list is None:
        return None

    days = []
    for day in multi.feed_daily_list:
        items = [
            {
                "time": getattr(item, "time", None),
                "name": getattr(item, "name", None),
                "amount": getattr(item, "amount", None),
                "amount1": getattr(item, "amount1", None),
                "amount2": getattr(item, "amount2", None),
                "id": getattr(item, "id", None),
            }
            for item in day.items or []
        ]
        days.append(
            {
                "repeats": getattr(day, "repeats", None),
                "suspended": getattr(day, "suspended", None),
                "count": len(items),
                "items": items,
            }
        )

    return {
        "device_id": feeder.id,
        "feed_daily_list": days,
    }


def get_raw_feed_plan_from_schedule(feeder) -> str | None:
    """Get the feed plan from the schedule definition (multi_feed_item).

    Unlike get_raw_feed_plan() which uses the execution log (growing unbounded),
    this reads the fixed-size schedule definition and cross-references with
    execution records for live status. Always under 255 chars for HA state limit.

    :param feeder: Feeder device object
    :return: A string with the feed plan in the format "id,hours,minutes,amount,state"
    """
    multi = getattr(feeder, "multi_feed_item", None)
    if multi is None or multi.feed_daily_list is None:
        return None

    # Use first day's schedule definition (all days share the same items)
    day = multi.feed_daily_list[0] if multi.feed_daily_list else None
    if day is None or not day.items:
        return None

    # Build execution status lookup from device_records.feed
    # Key: time_in_seconds → status code
    status_lookup = {}
    records = getattr(feeder, "device_records", None)
    if records and getattr(records, "feed", None):
        now = datetime.now()
        current_seconds = now.hour * 3600 + now.minute * 60 + now.second

        for feed in records.feed:
            for item in feed.items or []:
                t = getattr(item, "time", 0) or 0
                src = getattr(item, "src", 0) or 0
                status_val = getattr(item, "status", 0) or 0
                item_state = getattr(item, "state", None)

                state = 0  # pending
                if item_state is None and status_val == 0 and t < current_seconds:
                    state = 6  # unknown / past due
                elif status_val == 1:
                    state = 7  # cancelled
                elif item_state is not None:
                    err_code = getattr(item_state, "err_code", -1)
                    result_code = getattr(item_state, "result", -1)
                    if err_code == 0 and result_code == 0:
                        state = {1: 1, 3: 2, 4: 3}.get(src, 1)
                    elif err_code == 10 and result_code == 8:
                        state = 8  # skipped
                    else:
                        state = 9  # error
                # Only store if we have a definitive status (not just pending)
                if state != 0 or t not in status_lookup:
                    status_lookup[t] = state

    # Generate state string from schedule definition
    result = []
    for idx, item in enumerate(day.items):
        t = getattr(item, "time", 0) or 0
        hours = t // 3600
        minutes = (t % 3600) // 60
        a1 = getattr(item, "amount1", 0) or 0
        a2 = getattr(item, "amount2", 0) or 0
        amount = getattr(item, "amount", None)
        if amount is None or amount == 0:
            amount = a1 + a2
        state = status_lookup.get(t, 0)
        result.append(f"{idx},{hours},{minutes},{amount},{state}")

    return ",".join(result) if result else None


def get_raw_feed_plan(feeder_records_data) -> str | None:
    """Get the raw feed plan from feeder data.

    :param feeder_records_data: FeederRecordsData
    :return: A string with the feed plan in the format "id_incremental,hours,minutes,amount,state"
    where:
        - id_incremental: The incremental ID of the feed item
        - hours: Hours
        - minutes: Minutes
        - amount: The amount of food dispensed (for dual feeders, it will be the sum of amount1 and amount2)
        - state: The state of the food depending on the device type
            - 0: Food is pending (not dispensed yet)
            - 1: Food was dispensed successfully (by schedule)
            - 2: Food was dispensed successfully (by remote command on app)
            - 3: Food was dispensed successfully (by local command on feeder)
            - 6: Unknown state (probably disconnected)
            - 7: Food was cancelled
            - 8: Food was skipped due to SurplusControl (only for feeders with camera)
            - 9: Food was not dispensed due to an error

    """
    result = []

    if not feeder_records_data:
        LOGGER.debug("No feeder records data found")
        return None

    if feeder_records_data.feed is None:
        LOGGER.debug("No feed data found")
        return None

    # Heure actuelle en secondes depuis minuit
    now = datetime.now()
    current_seconds = now.hour * 3600 + now.minute * 60 + now.second

    for feed in feeder_records_data.feed:
        items = feed.items
        for idx, item in enumerate(items):
            id_incremental = idx
            time_in_seconds = item.time
            hours = time_in_seconds // 3600
            minutes = (time_in_seconds % 3600) // 60

            # Calculate amount
            amount = (
                item.amount
                if item.amount is not None
                else (getattr(item, "amount1", 0) or 0)
                + (getattr(item, "amount2", 0) or 0)
            )

            state = 0  # Pending by default

            if (
                (not hasattr(item, "state") or item.state is None)
                and item.status == 0
                and time_in_seconds < current_seconds
            ):
                state = 6

            elif item.status == 1:
                state = 7  # Food was cancelled
            elif hasattr(item, "state") and item.state is not None:
                if item.state.err_code == 0 and item.state.result == 0:
                    if item.src == 1:
                        state = 1
                    elif item.src == 3:
                        state = 2
                    elif item.src == 4:
                        state = 3
                    else:
                        state = 1
                elif item.state.err_code == 10 and item.state.result == 8:
                    state = 8
                else:
                    state = 9

            result.append(f"{id_incremental},{hours},{minutes},{amount},{state}")

    return ",".join(result) if result else None


def map_litter_event(litter_event: list[LitterRecord | None]) -> str | None:
    """Return a description of the last event.

    Use the 'litter_last_event' translation table to map the state to a human-readable string.
    """

    if not isinstance(litter_event, list) or not litter_event:
        return None

    litter_event = litter_event[-1]

    error = litter_event.content.error

    if litter_event.sub_content:
        event_type = litter_event.sub_content[-1].event_type
        result = litter_event.sub_content[-1].content.result
        reason = litter_event.sub_content[-1].content.start_reason
    else:
        return litter_event.enum_event_type

    if event_type not in [5, 6, 7, 8, 10]:
        LOGGER.debug("Unknown event type code: %s", event_type)
        return "event_type_unknown"

    if event_type == 10:
        name = "Unknown" if litter_event.pet_name is None else litter_event.pet_name
        return f"{name} used the litter box"

    try:
        if event_type == 5 and result == 2:
            return EVENT_MAPPING[event_type][result][reason][error]

        if event_type in [6, 7] and result == 2:
            return EVENT_MAPPING[event_type][result][error]

        if event_type in [8, 5]:
            return EVENT_MAPPING[event_type][result][reason]

        return EVENT_MAPPING[event_type][result]

    except KeyError:
        LOGGER.debug("Unknown event type result: %s", event_type)
        return f"event_type_{event_type}_unknown"


def get_dispense_status(
    feed_record: RecordsItems,
) -> tuple[str, str, int, int, int, int]:
    """Get the dispense status.

    :param feed_record: RecordsItems
    :return: tuple (source, status, plan_amount1, plan_amount2, disp_amount1, disp_amount2)
    """

    # Init
    plan_amount1 = getattr(feed_record, "amount", 0)
    plan_amount2 = 0
    disp_amount1 = 0
    disp_amount2 = 0

    # Déterminer les montants planifiés si `amount1` et `amount2` existent
    if hasattr(feed_record, "amount1") and hasattr(feed_record, "amount2"):
        plan_amount1 = feed_record.amount1
        plan_amount2 = feed_record.amount2

    # Find the source
    source_mapping = {
        1: "feeding plan",
        3: "manual (source : from application)",
        4: "manual (source : locally from feeder)",
    }
    source = source_mapping.get(feed_record.src, "unknown")

    # Find the status
    if feed_record.status == 1:
        status = "cancelled"
    elif hasattr(feed_record, "state") and feed_record.state is not None:
        state = feed_record.state
        if state.err_code == 0 and state.result == 0:
            status = "dispensed"
        elif state.err_code == 10 and state.result == 8:
            status = "skipped"
        else:
            status = "failed dispense"

        # Determinate the dispensed amount
        disp_amount1 = getattr(state, "real_amount", 0)
        disp_amount1 = getattr(state, "real_amount1", disp_amount1)
        disp_amount2 = getattr(state, "real_amount2", 0)
    else:
        status = "pending"

    return source, status, plan_amount1, plan_amount2, disp_amount1, disp_amount2


def get_device_records_history(device) -> dict[str, any] | None:
    """Get the full device records history as a dictionary.

    This includes subContent for each record, making it suitable for a
    Home Assistant sensor attribute.
    """
    records = getattr(device, "device_records", None)
    if not records:
        return None

    # pypetkitapi uses Pydantic. Use json serialization to ensure basic types
    serialized_records = []
    for record in records:
        try:
            if hasattr(record, "model_dump_json"):
                serialized_records.append(
                    json.loads(record.model_dump_json(by_alias=True, exclude_none=True))
                )
            elif hasattr(record, "json"):
                serialized_records.append(
                    json.loads(record.json(by_alias=True, exclude_none=True))
                )
            else:
                # Fallback if not a pydantic model
                serialized_records.append(vars(record))
        except (TypeError, ValueError) as e:
            LOGGER.error("Failed to serialize litter record: %s", e)

    return {"records": serialized_records[::-1]}
