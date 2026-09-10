"""OpenPetBowl schedule helpers for Petkit dry feeders.

Cloud ``repeats`` stay 1=Sunday inside OEM helpers. ISO 1=Monday…7=Sunday is the
entity/card contract.
"""

from __future__ import annotations

from datetime import datetime
from itertools import pairwise
from typing import Any

D4S = "d4s"
D4SH = "d4sh"
D4H = "d4h"
D3 = "d3"
D4 = "d4"
FEEDER = "feeder"
FEEDER_MINI = "feedermini"
DUAL_HOPPER_DEVICES = (D4S, D4SH)

# D1/D2 client-generated meal ids (wiki D2_PLAN_ITEM_ID).
_MINI_PLAN_ITEM_ID_START = 100001
_D2_MIN_GAP_SECS = 300

_SHAPE_A_TYPES = {FEEDER, FEEDER_MINI}
# App editors call suspendFeed/restoreFeed only for types 4/6/9/11. D4s/D4sh/D4h
# pause per day via the feedDailyList ``suspended`` flag instead.
_GLOBAL_TOGGLE_TYPES = {FEEDER, FEEDER_MINI, D3, D4}
# The app's plan editor for types 4/6/9/11 enforces both a non-empty name
# and the 5-minute gap for all of them.
_LEGACY_EDITOR_TYPES = {FEEDER, FEEDER_MINI, D3, D4}
# surplusControl skip is a ``state.result`` value, and it differs by family:
# the app reads 6 on D3 and 8 on D4sh/D4h.
_SURPLUS_RESULT_BY_TYPE = {D3: 6, D4SH: 8, D4H: 8}
# Results the app renders as dispensed for the D3-class families.
_DISPENSED_RESULTS = {0, 1, 2, 4, 5, 6, 9, 10, 11, 12, 13}
_GRAM_AMOUNT_TYPES = {D3}
# Families the app shows as a cup fraction rather than a portion count. Wire
# amounts are hundredths of a cup, so one portion is ``divisor / 100`` cup.
# The dual hoppers show bare portions and ignore ``factor`` for display.
_CUP_DISPLAY_TYPES = {FEEDER, FEEDER_MINI, D4, D4H}
_CUP_HUNDREDTHS = 100

# Wire ``amount`` is the displayed portion count times a per-family divisor
# (the app's editor reads it back as amount / divisor). D3 is grams
# and the dual hoppers are raw picker ticks, so neither scales. The card is fed
# display units and the conversion happens here, as it does in the app.
_AMOUNT_DIVISOR: dict[str, int] = {FEEDER: 20, FEEDER_MINI: 5}
_DEFAULT_D4_FACTOR = 10

_AMOUNT_BY_TYPE: dict[str, dict[str, int]] = {
    D3: {"min": 5, "max": 200, "step": 1},
    D4S: {"min": 0, "max": 10, "step": 1},
    D4SH: {"min": 0, "max": 10, "step": 1},
}

_DEFAULT_AMOUNT = {"min": 1, "max": 10, "step": 1}

CANONICAL_STATUSES = (
    "pending",
    "dispensed",
    "dispensing",
    "failed",
    "skipped",
    "disabled",
    "unknown",
)


def oem_weekday_to_iso(oem: int) -> int:
    """Convert PetKit weekday (1=Sunday) to ISO (1=Monday … 7=Sunday)."""
    if oem == 1:
        return 7
    return oem - 1


def iso_weekday_to_oem(iso: int) -> int:
    """Convert ISO weekday (1=Monday … 7=Sunday) to PetKit (1=Sunday)."""
    if iso == 7:
        return 1
    return iso + 1


def parse_oem_repeats(repeats: Any) -> list[int]:
    """Return sorted unique OEM weekday ints from csv or a single int."""
    if repeats is None or repeats == "":
        return [1, 2, 3, 4, 5, 6, 7]
    if isinstance(repeats, int):
        return [repeats] if 1 <= repeats <= 7 else []
    parts: list[int] = []
    for token in str(repeats).split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError:
            continue
        if 1 <= value <= 7:
            parts.append(value)
    return sorted(set(parts))


def feeder_device_type(feeder: Any) -> str:
    """Return lowercase PetKit prefix for this feeder."""
    nfo = getattr(feeder, "device_nfo", None)
    raw = getattr(nfo, "device_type", None) if nfo is not None else None
    return str(raw or "").lower()


def is_dual_hopper(feeder: Any) -> bool:
    """True for D4s / D4sh dual hoppers."""
    return feeder_device_type(feeder) in DUAL_HOPPER_DEVICES


def is_shape_a(feeder: Any) -> bool:
    """True for shared-mask FeederPlan families (D1 / Mini)."""
    return feeder_device_type(feeder) in _SHAPE_A_TYPES


def _amount_divisor(feeder: Any, device_type: str) -> int:
    """Portion size in wire units, or 1 when the family sends raw values."""
    if device_type in (D4, D4H):
        settings = getattr(feeder, "settings", None)
        factor = _coerce_int(getattr(settings, "factor", None), 0)
        return factor or _DEFAULT_D4_FACTOR
    return _AMOUNT_DIVISOR.get(device_type, 1)


def amount_config_for_feeder(feeder: Any) -> dict[str, Any]:
    """Amount bounds in the units the app shows the user."""
    device_type = feeder_device_type(feeder)
    amount = dict(_AMOUNT_BY_TYPE.get(device_type, _DEFAULT_AMOUNT))
    amount["unit"] = "g" if device_type in _GRAM_AMOUNT_TYPES else "portions"
    if device_type in _CUP_DISPLAY_TYPES:
        # Same shape as the card's ``alternate_unit`` option; YAML overrides it.
        divisor = _amount_divisor(feeder, device_type)
        amount["alternate_unit"] = {
            "unit_of_measurement": "cup",
            "conversion_factor": divisor / _CUP_HUNDREDTHS,
        }
    return amount


def _to_display_amount(value: Any, divisor: int) -> int:
    """Wire units -> portions, the way the editors read the value back."""
    return _coerce_int(value, 0) // divisor if divisor > 1 else _coerce_int(value, 0)


def _to_wire_amount(value: Any, divisor: int) -> int:
    """Portions -> wire units, the way the scale pickers emit them."""
    return _coerce_int(value, 0) * divisor


def capabilities_for_feeder(feeder: Any) -> dict[str, Any]:
    """Build the OpenPetBowl capabilities object for this family."""
    device_type = feeder_device_type(feeder)
    compartments = 2 if device_type in DUAL_HOPPER_DEVICES else 1
    amount = amount_config_for_feeder(feeder)
    today_skip = True
    global_toggle = device_type in _GLOBAL_TOGGLE_TYPES
    actions: dict[str, str] = {
        "set": "petkit.set_feeding_schedule",
        "add": "petkit.add_feeding_schedule_entry",
        "edit": "petkit.edit_feeding_schedule_entry",
        "remove": "petkit.remove_feeding_schedule_entry",
    }
    if today_skip:
        actions["skip_today"] = "petkit.skip_feeding_today"
        actions["unskip_today"] = "petkit.unskip_feeding_today"
    caps: dict[str, Any] = {
        "compartments": compartments,
        "amount": amount,
        "weekly": device_type not in _SHAPE_A_TYPES,
        "today_skip": today_skip,
        "global_toggle": global_toggle,
        "labels": {"required": device_type in _LEGACY_EDITOR_TYPES},
        "actions": actions,
    }
    return caps


def _item_attr(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _coerce_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _item_values(item: Any, dual: bool, divisor: int = 1) -> list[int]:
    """Displayed amounts for one meal. Dual hoppers never scale."""
    if dual:
        return [
            _coerce_int(_item_attr(item, "amount1"), 0),
            _coerce_int(_item_attr(item, "amount2"), 0),
        ]
    return [_to_display_amount(_item_attr(item, "amount"), divisor)]


def _item_key(item: Any) -> str:
    """Group meals by time, never by ``id``.

    The app keys a meal by (day, time) and forbids duplicate times within a
    day. Item ids are not a per-meal identity: the copy-to-other-days screen
    clones them verbatim, and they are only rewritten when 0, so they go stale
    as soon as a copied meal's time is edited.
    """
    return str(_coerce_int(_item_attr(item, "time"), 0))


def _ids_by_key(days: list[dict[str, Any]]) -> dict[str, int]:
    """Server-assigned ids of the meals already in the plan, keyed by time."""
    out: dict[str, int] = {}
    for day in days:
        for item in day.get("items") or []:
            item_id = _coerce_int(_item_attr(item, "id"), 0)
            if item_id:
                out.setdefault(_item_key(item), item_id)
    return out


def _serialize_item(item: Any) -> dict[str, Any]:
    return {
        "time": _coerce_int(_item_attr(item, "time"), 0),
        "name": _item_attr(item, "name", None),
        "amount": _item_attr(item, "amount", None),
        "amount1": _item_attr(item, "amount1", None),
        "amount2": _item_attr(item, "amount2", None),
        "id": _item_attr(item, "id", None),
        "deviceId": _item_attr(item, "device_id", _item_attr(item, "deviceId", 0)),
        "deviceType": _item_attr(
            item, "device_type", _item_attr(item, "deviceType", 0)
        ),
        "petAmount": _item_attr(item, "pet_amount", _item_attr(item, "petAmount", []))
        or [],
    }


_NATIVE_BY_SRC = {
    1: "dispensed_schedule",
    2: "dispensed_remote",
    3: "dispensed_remote",
    4: "dispensed_local",
}


def _state_attr(state: Any, snake: str, camel: str) -> Any:
    return _item_attr(state, snake, _item_attr(state, camel))


def _real_amount(state: Any) -> int:
    """Food actually dispensed, across both hoppers."""
    return sum(
        _coerce_int(_state_attr(state, snake, camel), 0)
        for snake, camel in (
            ("real_amount", "realAmount"),
            ("real_amount1", "realAmount1"),
            ("real_amount2", "realAmount2"),
        )
    )


def _shape_a_status(state: Any, src: int) -> tuple[str, str | None]:
    """D1/D2 have no ``result``: success is empty errCode + a completedAt.

    ``FeederItemDataViewHolder`` — errCode is a string there ("ia-1", "em-1"),
    and manual-offline meals (src 4) report no completedAt.
    """
    err = _state_attr(state, "err_code", "errCode")
    if err is not None and str(err).strip() not in ("", "0"):
        return "failed", "error"
    completed = _state_attr(state, "completed_at", "completedAt")
    if (completed is not None and str(completed).strip()) or src == 4:
        return "dispensed", _NATIVE_BY_SRC.get(src, "dispensed_schedule")
    return "pending", None


def _d3_class_status(state: Any, src: int, device_type: str) -> tuple[str, str | None]:
    """D3/D4-class meals are classified by ``state.result``, not errCode."""
    result = _coerce_int(_state_attr(state, "result", "result"), -1)
    surplus = _SURPLUS_RESULT_BY_TYPE.get(device_type)
    if surplus is not None and result == surplus:
        return "skipped", "surplus_skipped"
    if result == 7:
        return "skipped", "cancelled"
    if result in _DISPENSED_RESULTS and _real_amount(state) > 0:
        return "dispensed", _NATIVE_BY_SRC.get(src, "dispensed_schedule")
    return "failed", "error"


def _status_from_record(
    item: Any, now_secs: int, *, device_type: str = ""
) -> tuple[str, str | None]:
    """Return (canonical_status, native_status) from a daily feed record item."""
    status_val = _coerce_int(_item_attr(item, "status"), 0)
    src = _coerce_int(_item_attr(item, "src"), 0)
    time_sec = _coerce_int(_item_attr(item, "time"), 0)
    state = _item_attr(item, "state", None)

    if status_val == 3:
        return "dispensing", None
    if status_val == 1:
        return "skipped", "cancelled"
    if status_val == 2:
        # Overdue today, resumes tomorrow (Feeder_item_not_start_prompt).
        return "unknown", "past_unknown"

    if state is None:
        if time_sec < now_secs:
            return "unknown", "past_unknown"
        return "pending", None

    if device_type in _SHAPE_A_TYPES:
        return _shape_a_status(state, src)
    return _d3_class_status(state, src, device_type)


def _today_record_status_by_time(
    feeder: Any,
) -> dict[int, tuple[str, str | None, str | None]]:
    """Map seconds-since-midnight → (status, native_status, daily_id)."""
    out: dict[int, tuple[str, str | None, str | None]] = {}
    records = getattr(feeder, "device_records", None)
    feed = getattr(records, "feed", None) if records is not None else None
    if not feed:
        return out
    device_type = feeder_device_type(feeder)
    now = datetime.now()
    now_secs = now.hour * 3600 + now.minute * 60 + now.second
    for block in feed:
        for item in getattr(block, "items", None) or []:
            time_sec = _coerce_int(_item_attr(item, "time"), -1)
            if time_sec < 0:
                continue
            status, native = _status_from_record(
                item, now_secs, device_type=device_type
            )
            daily_id = _item_attr(item, "id", None)
            out[time_sec] = (
                status,
                native,
                None if daily_id is None else str(daily_id),
            )
    return out


def is_feeding_plan_enabled(feeder: Any) -> bool:
    """True when the recurring plan is not globally suspended."""
    plan = getattr(feeder, "feed_plan", None)
    if plan is not None:
        days = getattr(plan, "feed_daily_list", None)
        if days:
            return not all(_coerce_int(getattr(d, "suspended", 0), 0) for d in days)
        return _coerce_int(getattr(plan, "suspended", 0), 0) == 0
    multi = getattr(feeder, "multi_feed_item", None)
    days = getattr(multi, "feed_daily_list", None) if multi is not None else None
    if days:
        return not all(_coerce_int(getattr(d, "suspended", 0), 0) for d in days)
    return True


def _days_from_feed_plan(plan: Any) -> list[dict[str, Any]] | None:
    if plan is None:
        return None
    days = getattr(plan, "feed_daily_list", None)
    if days is not None:
        out = []
        for day in days:
            items = [_serialize_item(it) for it in getattr(day, "items", None) or []]
            out.append(
                {
                    "repeats": getattr(day, "repeats", None),
                    "suspended": getattr(day, "suspended", 0),
                    "count": len(items),
                    "items": items,
                }
            )
        return out
    items_raw = getattr(plan, "items", None)
    if items_raw is None:
        return None
    items = [_serialize_item(it) for it in items_raw]
    mask = set(parse_oem_repeats(getattr(plan, "repeats", None)))
    suspended = _coerce_int(getattr(plan, "suspended", 0), 0)
    out = []
    for oem in range(1, 8):
        if oem in mask:
            out.append(
                {
                    "repeats": oem,
                    "suspended": suspended,
                    "count": len(items),
                    "items": [dict(it) for it in items],
                }
            )
        else:
            out.append({"repeats": oem, "suspended": 0, "count": 0, "items": []})
    return out


def _fallback_schedule_pairs(feeder: Any) -> list[tuple[int, int]]:
    """Last-resort slots from feedTimes / records (utils), or empty when standalone."""
    try:
        from .utils import resolve_feed_schedule_pairs
    except (ImportError, ModuleNotFoundError):
        return []
    return resolve_feed_schedule_pairs(feeder)


def oem_days_from_feed_plan(feeder: Any) -> list[dict[str, Any]] | None:
    """Return OEM days from GET ``feed`` if the library attached ``feed_plan``."""
    return _days_from_feed_plan(getattr(feeder, "feed_plan", None))


def feed_daily_list_from_feeder(feeder: Any) -> list[dict[str, Any]]:
    """OEM 7-day list (repeats 1=Sunday) from GET feed, else existing fallbacks."""
    days = oem_days_from_feed_plan(feeder)
    if days is not None:
        return days

    multi = getattr(feeder, "multi_feed_item", None)
    if multi is not None and getattr(multi, "feed_daily_list", None) is not None:
        out = []
        for day in multi.feed_daily_list:
            items = [_serialize_item(it) for it in day.items or []]
            out.append(
                {
                    "repeats": getattr(day, "repeats", None),
                    "suspended": getattr(day, "suspended", 0),
                    "count": len(items),
                    "items": items,
                }
            )
        return out

    pairs = _fallback_schedule_pairs(feeder)
    if not pairs:
        return [
            {"repeats": oem, "suspended": 0, "count": 0, "items": []}
            for oem in range(1, 8)
        ]
    synth = [
        {"time": t_sec, "name": None, "amount": amt, "id": str(t_sec)}
        for t_sec, amt in pairs
    ]
    return [
        {
            "repeats": oem,
            "suspended": 0,
            "count": len(synth),
            "items": [dict(it) for it in synth],
        }
        for oem in range(1, 8)
    ]


def flatten_schedule(feeder: Any) -> list[dict[str, Any]]:
    """Flatten OEM plan days into OpenPetBowl rows (ISO weekdays)."""
    dual = is_dual_hopper(feeder)
    divisor = _amount_divisor(feeder, feeder_device_type(feeder))
    days = feed_daily_list_from_feeder(feeder)
    status_by_time = _today_record_status_by_time(feeder)
    today_iso = datetime.now().isoweekday()
    plan_enabled = is_feeding_plan_enabled(feeder)

    groups: dict[str, dict[str, Any]] = {}
    for day in days:
        oem_days = parse_oem_repeats(day.get("repeats"))
        day_suspended = _coerce_int(day.get("suspended"), 0) == 1
        for item in day.get("items") or []:
            time_sec = _coerce_int(item.get("time"), 0)
            name = (item.get("name") or "") or ""
            values = _item_values(item, dual, divisor)
            key = _item_key(item)
            group = groups.get(key)
            if group is None:
                group = {
                    "key": key,
                    "hour": time_sec // 3600,
                    "minute": (time_sec % 3600) // 60,
                    "values": values,
                    "label": name,
                    "oem_days": set(),
                    "suspended": True,
                }
                groups[key] = group
            if not day_suspended:
                group["suspended"] = False
            for oem in oem_days:
                group["oem_days"].add(oem)

    rows: list[dict[str, Any]] = []
    for group in groups.values():
        iso_days = sorted(oem_weekday_to_iso(o) for o in group["oem_days"])
        time_sec = group["hour"] * 3600 + group["minute"] * 60
        rec = status_by_time.get(time_sec)
        if not plan_enabled or group["suspended"]:
            status, native = "disabled", None
        elif rec:
            status, native = rec[0], rec[1]
        else:
            status, native = "pending", None
        # D1/Mini: the card has no per-meal weekday UI, because the app has
        # none either — the mask is one plan-level Repeat control. Rows carry no
        # weekdays, but ``today`` still reports whether the plan runs today, so
        # skip-today is not offered on a day the meal will not fire.
        row_weekdays = None if is_shape_a(feeder) else iso_days
        rows.append(
            {
                "key": group["key"],
                "hour": group["hour"],
                "minute": group["minute"],
                "values": group["values"],
                "weekdays": row_weekdays,
                "label": group["label"],
                "status": status,
                "native_status": native,
                "enabled": not group["suspended"] and plan_enabled,
                "today": today_iso in iso_days,
                "readonly": False,
            }
        )
    rows.sort(key=lambda r: (r["hour"], r["minute"], r["key"]))
    return rows


def plan_weekdays_iso(feeder: Any) -> list[int] | None:
    """ISO weekdays of the D1/Mini plan-level mask, or None if not Shape A.

    Provider-specific, deliberately outside the OpenPetBowl contract: the app
    edits this as one Repeat control covering every meal, and there is no
    per-meal equivalent to map it onto. Exposed so automations and other
    clients can read and set it; the card ignores it.
    """
    if not is_shape_a(feeder):
        return None
    _items, mask, _suspended = _shape_a_items_and_mask(
        feed_daily_list_from_feeder(feeder)
    )
    if not mask:
        return None
    return sorted(oem_weekday_to_iso(oem) for oem in mask)


def set_plan_weekdays(feeder: Any, weekdays: list[int]) -> str:
    """Validate an ISO weekday list and return the OEM csv the cloud wants."""
    if not is_shape_a(feeder):
        raise ValueError(
            "only the D1 and Mini have a plan-level weekday mask; on this "
            "family set weekdays per meal instead"
        )
    mask = sorted({iso_weekday_to_oem(int(iso)) for iso in weekdays})
    if not mask:
        raise ValueError("weekdays must name at least one day")
    return ",".join(str(oem) for oem in mask)


def get_openpetbowl_attributes(feeder: Any) -> dict[str, Any]:
    """Attributes for the feeding-plan entity (no marker)."""
    attributes: dict[str, Any] = {
        "device_id": getattr(feeder, "id", None),
        "capabilities": capabilities_for_feeder(feeder),
        "schedule": flatten_schedule(feeder),
    }
    plan_weekdays = plan_weekdays_iso(feeder)
    if plan_weekdays is not None:
        attributes["plan_weekdays"] = plan_weekdays
    return attributes


def _empty_oem_week() -> list[dict[str, Any]]:
    return [
        {"repeats": str(oem), "suspended": 0, "count": 0, "items": []}
        for oem in range(1, 8)
    ]


def _item_from_row(
    row: dict[str, Any],
    dual: bool,
    *,
    item_id: int | None = None,
    divisor: int = 1,
) -> dict[str, Any]:
    """One OEM meal item. ``id`` 0 means "new" — the library rewrites it."""
    time_sec = int(row["hour"]) * 3600 + int(row["minute"]) * 60
    values = list(row.get("values") or [])
    item: dict[str, Any] = {
        "time": time_sec,
        "name": row.get("label") or "",
        "petAmount": row.get("petAmount") or [],
        "id": item_id or 0,
    }
    if dual:
        item["amount"] = 0
        item["amount1"] = int(values[0]) if len(values) > 0 else 0
        item["amount2"] = int(values[1]) if len(values) > 1 else 0
    else:
        item["amount"] = _to_wire_amount(values[0], divisor) if values else 0
        item["amount1"] = 0
        item["amount2"] = 0
    return item


def schedule_to_feed_daily_list(
    schedule: list[dict[str, Any]],
    feeder: Any,
    previous: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Convert OpenPetBowl ISO rows into OEM 7-day feedDailyList."""
    dual = is_dual_hopper(feeder)
    divisor = _amount_divisor(feeder, feeder_device_type(feeder))
    prev = previous if previous is not None else feed_daily_list_from_feeder(feeder)
    known_ids = _ids_by_key(prev)
    if is_shape_a(feeder):
        _ignored, mask, suspended = _shape_a_items_and_mask(prev)
        items = [
            _item_from_row(
                row,
                dual,
                item_id=known_ids.get(str(row.get("key"))),
                divisor=divisor,
            )
            for row in schedule
        ]
        if not mask:
            mask = {1, 2, 3, 4, 5, 6, 7}
        return _expand_shape_a_days(items, mask, suspended)

    days = _empty_oem_week()
    if previous:
        by_oem = {}
        for day in previous:
            oem_days = parse_oem_repeats(day.get("repeats"))
            for oem in oem_days:
                by_oem[oem] = day
        for oem in range(1, 8):
            prev_day = by_oem.get(oem)
            if prev_day is not None:
                days[oem - 1]["suspended"] = _coerce_int(prev_day.get("suspended"), 0)

    for row in schedule:
        iso_days = row.get("weekdays") or [1, 2, 3, 4, 5, 6, 7]
        item = _item_from_row(
            row, dual, item_id=known_ids.get(str(row.get("key"))), divisor=divisor
        )
        for iso in iso_days:
            oem = iso_weekday_to_oem(int(iso))
            day = days[oem - 1]
            day["items"].append(dict(item))
    for day in days:
        day["items"].sort(key=lambda it: it.get("time") or 0)
        day["count"] = len(day["items"])
        if dual:
            day["totalAmount"] = 0
            day["totalAmount1"] = sum(
                _coerce_int(it.get("amount1"), 0) for it in day["items"]
            )
            day["totalAmount2"] = sum(
                _coerce_int(it.get("amount2"), 0) for it in day["items"]
            )
        else:
            day["totalAmount"] = sum(
                _coerce_int(it.get("amount"), 0) for it in day["items"]
            )
            day["totalAmount1"] = 0
            day["totalAmount2"] = 0
    return days


def _next_mini_id(days: list[dict[str, Any]]) -> int:
    max_id = _MINI_PLAN_ITEM_ID_START - 1
    for day in days:
        for item in day.get("items") or []:
            try:
                max_id = max(max_id, int(item.get("id") or 0))
            except (TypeError, ValueError):
                continue
    return max(max_id + 1, _MINI_PLAN_ITEM_ID_START)


def validate_row(
    feeder: Any,
    days: list[dict[str, Any]],
    hour: int,
    minute: int,
    values: list[int],
    weekdays: list[int],
    *,
    skip_key: str | None = None,
) -> None:
    """Reject duplicate times, D2 gaps, and dual arity mismatches."""
    caps = capabilities_for_feeder(feeder)
    if len(values) != caps["compartments"]:
        raise ValueError(
            f"values length {len(values)} does not match compartments "
            f"{caps['compartments']}"
        )
    time_sec = hour * 3600 + minute * 60
    iso_days = weekdays or [1, 2, 3, 4, 5, 6, 7]
    oem_want = {iso_weekday_to_oem(i) for i in iso_days}
    existing_times: list[int] = []
    for day in days:
        oem_days = set(parse_oem_repeats(day.get("repeats")))
        if not oem_days.intersection(oem_want):
            continue
        for item in day.get("items") or []:
            if skip_key is not None and _item_key(item) == str(skip_key):
                continue
            existing_times.append(_coerce_int(item.get("time"), 0))
    if time_sec in existing_times:
        raise ValueError("duplicate hour+minute on the same weekday set")
    # The app applies the 5-minute gap check ungated in the editor that serves
    # types 4/6/9/11, so D1, D3 and D4 get the rule too, not only the Mini.
    if feeder_device_type(feeder) in _LEGACY_EDITOR_TYPES:
        merged = sorted({*existing_times, time_sec})
        for left, right in pairwise(merged):
            if right - left < _D2_MIN_GAP_SECS:
                raise ValueError("meals must be at least 300 seconds apart")


def _shape_a_items_and_mask(
    days: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[int], int]:
    """Deduplicate Shape A items, and collect the mask and the pause flag."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    mask: set[int] = set()
    suspended = 0
    for day in days:
        slot = day.get("items") or []
        if not slot:
            continue
        mask.update(parse_oem_repeats(day.get("repeats")))
        if _coerce_int(day.get("suspended"), 0):
            suspended = 1
        for item in slot:
            key = _item_key(item)
            if key in seen:
                continue
            seen.add(key)
            items.append(dict(item))
    return items, mask, suspended


def add_schedule_entry(
    feeder: Any,
    hour: int,
    minute: int,
    values: list[int],
    weekdays: list[int] | None = None,
    label: str | None = None,
) -> list[dict[str, Any]]:
    """Merge one new row into the current OEM plan."""
    days = feed_daily_list_from_feeder(feeder)
    iso_days = weekdays or [1, 2, 3, 4, 5, 6, 7]
    if is_shape_a(feeder):
        _existing_items, existing_oem, _susp = _shape_a_items_and_mask(days)
        if existing_oem:
            iso_days = [oem_weekday_to_iso(o) for o in existing_oem]
    validate_row(feeder, days, hour, minute, values, iso_days)
    dual = is_dual_hopper(feeder)
    allocate = _next_mini_id(days) if is_shape_a(feeder) else None
    item = _item_from_row(
        {"hour": hour, "minute": minute, "values": values, "label": label or ""},
        dual,
        item_id=allocate,
        divisor=_amount_divisor(feeder, feeder_device_type(feeder)),
    )
    oem_want = {iso_weekday_to_oem(i) for i in iso_days}
    if is_shape_a(feeder):
        existing_items, existing_oem, suspended = _shape_a_items_and_mask(days)
        existing_items.append(dict(item))
        # One meal list × one weekday mask. New meals join the plan; they
        # cannot introduce a different day set.
        mask = existing_oem or oem_want
        return _expand_shape_a_days(existing_items, mask, suspended)
    for day in days:
        oem = parse_oem_repeats(day.get("repeats"))
        if oem and oem[0] in oem_want:
            day["items"].append(dict(item))
            day["count"] = len(day["items"])
    return days


def _expand_shape_a_days(
    items: list[dict[str, Any]], oem_mask: set[int], suspended: int = 0
) -> list[dict[str, Any]]:
    """One meal list × weekday mask expanded to 7 OEM days.

    A day outside the mask carries an EMPTY ``repeats``; the library rebuilds the
    plan mask from the days that have one, so clearing every meal still preserves
    it. Encoding "not in the mask" as ``suspended`` would collide with the
    whole-plan pause, which is a separate flag on the same save.
    """
    out: list[dict[str, Any]] = []
    for oem in range(1, 8):
        if oem in oem_mask:
            copied = [dict(it) for it in items]
            out.append(
                {
                    "repeats": str(oem),
                    "suspended": suspended,
                    "count": len(copied),
                    "items": copied,
                }
            )
        else:
            out.append({"repeats": "", "suspended": suspended, "count": 0, "items": []})
    return out


def edit_schedule_entry(
    feeder: Any,
    key: str,
    hour: int,
    minute: int,
    values: list[int],
    weekdays: list[int] | None = None,
    label: str | None = None,
) -> list[dict[str, Any]]:
    """Patch the matching row then rewrite the OEM plan."""
    days = feed_daily_list_from_feeder(feeder)
    iso_days = weekdays or [1, 2, 3, 4, 5, 6, 7]
    if is_shape_a(feeder) and weekdays is None:
        _items, existing_oem, _susp = _shape_a_items_and_mask(days)
        if existing_oem:
            iso_days = [oem_weekday_to_iso(o) for o in existing_oem]
    validate_row(feeder, days, hour, minute, values, iso_days, skip_key=key)
    dual = is_dual_hopper(feeder)
    # Keep the server id of the meal being edited; only its fields change.
    new_item = _item_from_row(
        {"hour": hour, "minute": minute, "values": values, "label": label or ""},
        dual,
        item_id=_ids_by_key(days).get(str(key)),
        divisor=_amount_divisor(feeder, feeder_device_type(feeder)),
    )
    oem_want = {iso_weekday_to_oem(i) for i in iso_days}
    if is_shape_a(feeder):
        existing_items, existing_oem, suspended = _shape_a_items_and_mask(days)
        kept = [it for it in existing_items if _item_key(it) != str(key)]
        if len(kept) == len(existing_items):
            raise ValueError(f"schedule key {key!r} not found")
        kept.append(dict(new_item))
        mask = existing_oem if weekdays is None else oem_want
        if not mask:
            mask = oem_want
        return _expand_shape_a_days(kept, mask, suspended)
    found = False
    for day in days:
        kept_day: list[dict[str, Any]] = []
        for item in day.get("items") or []:
            if _item_key(item) == str(key):
                found = True
                continue
            kept_day.append(item)
        day["items"] = kept_day
        oem = parse_oem_repeats(day.get("repeats"))
        if oem and oem[0] in oem_want:
            day["items"].append(dict(new_item))
        day["count"] = len(day["items"])
    if not found:
        raise ValueError(f"schedule key {key!r} not found")
    return days


def remove_schedule_entry(feeder: Any, key: str) -> list[dict[str, Any]]:
    """Drop the matching row from every OEM day."""
    days = feed_daily_list_from_feeder(feeder)
    if is_shape_a(feeder):
        existing_items, mask, suspended = _shape_a_items_and_mask(days)
        kept = [it for it in existing_items if _item_key(it) != str(key)]
        if len(kept) == len(existing_items):
            raise ValueError(f"schedule key {key!r} not found")
        return _expand_shape_a_days(kept, mask, suspended)
    found = False
    for day in days:
        kept = []
        for item in day.get("items") or []:
            if _item_key(item) == str(key):
                found = True
                continue
            kept.append(item)
        day["items"] = kept
        day["count"] = len(kept)
    if not found:
        raise ValueError(f"schedule key {key!r} not found")
    return days


def daily_record_id_for_key(feeder: Any, key: str) -> str | None:
    """Today's daily-feed id for skip/unskip, matched by schedule key time."""
    rows = flatten_schedule(feeder)
    match = next((r for r in rows if r["key"] == str(key)), None)
    if match is None:
        return None
    time_sec = match["hour"] * 3600 + match["minute"] * 60
    rec = _today_record_status_by_time(feeder).get(time_sec)
    if rec is None:
        return None
    return rec[2]
