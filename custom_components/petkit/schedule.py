"""OpenPetBowl schedule helpers for Petkit dry feeders.

Cloud ``repeats`` stay 1=Sunday inside OEM helpers. ISO 1=Monday…7=Sunday is the
entity/card contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

D4S = "d4s"
D4SH = "d4sh"
FEEDER = "feeder"
FEEDER_MINI = "feedermini"
DUAL_HOPPER_DEVICES = (D4S, D4SH)

# D1/D2 client-generated meal ids (wiki D2_PLAN_ITEM_ID).
_MINI_PLAN_ITEM_ID_START = 100001
_D2_MIN_GAP_SECS = 300

_SHAPE_A_TYPES = {FEEDER, FEEDER_MINI}

_AMOUNT_BY_TYPE: dict[str, dict[str, int]] = {
    FEEDER: {"min": 1, "max": 10, "step": 1},
    FEEDER_MINI: {"min": 5, "max": 50, "step": 5},
    "d3": {"min": 5, "max": 200, "step": 1},
    "d4": {"min": 10, "max": 50, "step": 10},
    "d4h": {"min": 10, "max": 50, "step": 10},
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
    """True for weekly FeederPlan families (D1 / Mini)."""
    return feeder_device_type(feeder) in _SHAPE_A_TYPES


def capabilities_for_feeder(feeder: Any) -> dict[str, Any]:
    """Build the OpenPetBowl capabilities object."""
    device_type = feeder_device_type(feeder)
    compartments = 2 if device_type in (D4S, D4SH) else 1
    amount = dict(_AMOUNT_BY_TYPE.get(device_type, _DEFAULT_AMOUNT))
    return {
        "compartments": compartments,
        "amount": amount,
        "weekly": True,
        "today_skip": True,
        "global_toggle": True,
        "labels": True,
        "actions": {
            "set": "petkit.set_feeding_schedule",
            "add": "petkit.add_feeding_schedule_entry",
            "edit": "petkit.edit_feeding_schedule_entry",
            "remove": "petkit.remove_feeding_schedule_entry",
            "skip_today": "petkit.skip_feeding_today",
            "unskip_today": "petkit.unskip_feeding_today",
        },
    }


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


def _item_values(item: Any, dual: bool) -> list[int]:
    if dual:
        return [
            _coerce_int(_item_attr(item, "amount1"), 0),
            _coerce_int(_item_attr(item, "amount2"), 0),
        ]
    amount = _item_attr(item, "amount", None)
    if amount is None:
        a1 = _coerce_int(_item_attr(item, "amount1"), 0)
        a2 = _coerce_int(_item_attr(item, "amount2"), 0)
        return [a1 + a2]
    return [_coerce_int(amount, 0)]


def _item_key(item: Any) -> str:
    item_id = _item_attr(item, "id", None)
    if item_id is not None and str(item_id) != "":
        return str(item_id)
    time_sec = _coerce_int(_item_attr(item, "time"), 0)
    name = _item_attr(item, "name", "") or ""
    return f"{time_sec}:{name}"


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


def _status_from_record(item: Any, now_secs: int) -> tuple[str, str | None]:
    """Return (canonical_status, native_status) from a daily feed record item."""
    status_val = _coerce_int(_item_attr(item, "status"), 0)
    src = _coerce_int(_item_attr(item, "src"), 0)
    time_sec = _coerce_int(_item_attr(item, "time"), 0)
    state = _item_attr(item, "state", None)

    if status_val == 3:
        return "dispensing", None
    if status_val == 1:
        return "skipped", "cancelled"

    if state is None:
        if status_val == 0 and time_sec < now_secs:
            return "unknown", "past_unknown"
        return "pending", None

    err_code = _coerce_int(_item_attr(state, "err_code", _item_attr(state, "errCode")), -1)
    result_code = _coerce_int(_item_attr(state, "result"), -1)
    if err_code == 0 and result_code == 0:
        native = {
            1: "dispensed_schedule",
            2: "dispensed_remote",
            3: "dispensed_remote",
            4: "dispensed_local",
        }.get(src, "dispensed_schedule")
        return "dispensed", native
    if err_code == 10 and result_code == 8:
        return "skipped", "surplus_skipped"
    return "failed", "error"


def _today_record_status_by_time(feeder: Any) -> dict[int, tuple[str, str | None, str | None]]:
    """Map seconds-since-midnight → (status, native_status, daily_id)."""
    out: dict[int, tuple[str, str | None, str | None]] = {}
    records = getattr(feeder, "device_records", None)
    feed = getattr(records, "feed", None) if records is not None else None
    if not feed:
        return out
    now = datetime.now()
    now_secs = now.hour * 3600 + now.minute * 60 + now.second
    for block in feed:
        for item in getattr(block, "items", None) or []:
            time_sec = _coerce_int(_item_attr(item, "time"), -1)
            if time_sec < 0:
                continue
            status, native = _status_from_record(item, now_secs)
            daily_id = _item_attr(item, "id", None)
            out[time_sec] = (status, native, None if daily_id is None else str(daily_id))
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
            values = _item_values(item, dual)
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
        rows.append(
            {
                "key": group["key"],
                "hour": group["hour"],
                "minute": group["minute"],
                "values": group["values"],
                "weekdays": iso_days,
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


def get_openpetbowl_attributes(feeder: Any) -> dict[str, Any]:
    """Attributes for the feeding-plan switch (no marker)."""
    days = feed_daily_list_from_feeder(feeder)
    return {
        "device_id": getattr(feeder, "id", None),
        "capabilities": capabilities_for_feeder(feeder),
        "schedule": flatten_schedule(feeder),
        "feed_daily_list": days,
    }


def _empty_oem_week() -> list[dict[str, Any]]:
    return [
        {"repeats": str(oem), "suspended": 0, "count": 0, "items": []}
        for oem in range(1, 8)
    ]


def _item_from_row(
    row: dict[str, Any],
    dual: bool,
    *,
    key: str | None = None,
    allocate_id: int | None = None,
) -> dict[str, Any]:
    time_sec = int(row["hour"]) * 3600 + int(row["minute"]) * 60
    values = list(row.get("values") or [])
    item: dict[str, Any] = {
        "time": time_sec,
        "name": row.get("label") or "",
        "petAmount": [],
        "deviceId": 0,
        "deviceType": 0,
    }
    if dual:
        item["amount"] = 0
        item["amount1"] = int(values[0]) if len(values) > 0 else 0
        item["amount2"] = int(values[1]) if len(values) > 1 else 0
    else:
        item["amount"] = int(values[0]) if values else 0
        item["amount1"] = 0
        item["amount2"] = 0
    if allocate_id is not None:
        item["id"] = allocate_id
    elif key is not None:
        try:
            item["id"] = int(key)
        except (TypeError, ValueError):
            item["id"] = time_sec
    else:
        item["id"] = time_sec
    return item


def schedule_to_feed_daily_list(
    schedule: list[dict[str, Any]],
    feeder: Any,
    previous: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Convert OpenPetBowl ISO rows into OEM 7-day feedDailyList."""
    dual = is_dual_hopper(feeder)
    days = _empty_oem_week()
    if previous:
        by_oem = {}
        for day in previous:
            oem_days = parse_oem_repeats(day.get("repeats"))
            for oem in oem_days:
                by_oem[oem] = day
        for oem in range(1, 8):
            prev = by_oem.get(oem)
            if prev is not None:
                days[oem - 1]["suspended"] = _coerce_int(prev.get("suspended"), 0)

    for row in schedule:
        iso_days = row.get("weekdays") or [1, 2, 3, 4, 5, 6, 7]
        item = _item_from_row(row, dual, key=row.get("key"))
        for iso in iso_days:
            oem = iso_weekday_to_oem(int(iso))
            day = days[oem - 1]
            day["items"].append(dict(item))
    for day in days:
        day["items"].sort(key=lambda it: it.get("time") or 0)
        day["count"] = len(day["items"])
        if dual:
            day["totalAmount"] = 0
            day["totalAmount1"] = sum(_coerce_int(it.get("amount1"), 0) for it in day["items"])
            day["totalAmount2"] = sum(_coerce_int(it.get("amount2"), 0) for it in day["items"])
        else:
            day["totalAmount"] = sum(_coerce_int(it.get("amount"), 0) for it in day["items"])
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
            if skip_key is not None and str(item.get("id")) == str(skip_key):
                continue
            existing_times.append(_coerce_int(item.get("time"), 0))
    if time_sec in existing_times:
        raise ValueError("duplicate hour+minute on the same weekday set")
    if feeder_device_type(feeder) == FEEDER_MINI:
        merged = sorted(set(existing_times + [time_sec]))
        for left, right in zip(merged, merged[1:], strict=False):
            if right - left < _D2_MIN_GAP_SECS:
                raise ValueError("Mini meals must be at least 300 seconds apart")


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
    validate_row(feeder, days, hour, minute, values, iso_days)
    dual = is_dual_hopper(feeder)
    allocate = _next_mini_id(days) if is_shape_a(feeder) else None
    item = _item_from_row(
        {"hour": hour, "minute": minute, "values": values, "label": label or ""},
        dual,
        allocate_id=allocate,
    )
    oem_want = {iso_weekday_to_oem(i) for i in iso_days}
    if is_shape_a(feeder):
        existing_items: list[dict[str, Any]] = []
        existing_oem: set[int] = set()
        for day in days:
            oem = parse_oem_repeats(day.get("repeats"))
            if day.get("items") and not existing_items:
                existing_items = [dict(it) for it in day["items"]]
            if day.get("items"):
                existing_oem.update(oem)
        existing_items.append(dict(item))
        mask = existing_oem | oem_want
        return _expand_shape_a_days(existing_items, mask)
    for day in days:
        oem = parse_oem_repeats(day.get("repeats"))
        if oem and oem[0] in oem_want:
            day["items"].append(dict(item))
            day["count"] = len(day["items"])
    return days


def _expand_shape_a_days(
    items: list[dict[str, Any]], oem_mask: set[int]
) -> list[dict[str, Any]]:
    """One meal list × weekday mask expanded to 7 OEM days."""
    out: list[dict[str, Any]] = []
    for oem in range(1, 8):
        if oem in oem_mask:
            copied = [dict(it) for it in items]
            out.append(
                {
                    "repeats": str(oem),
                    "suspended": 0,
                    "count": len(copied),
                    "items": copied,
                }
            )
        else:
            out.append(
                {"repeats": str(oem), "suspended": 0, "count": 0, "items": []}
            )
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
    validate_row(feeder, days, hour, minute, values, iso_days, skip_key=key)
    dual = is_dual_hopper(feeder)
    new_item = _item_from_row(
        {
            "hour": hour,
            "minute": minute,
            "values": values,
            "label": label or "",
            "key": key,
        },
        dual,
        key=key,
    )
    oem_want = {iso_weekday_to_oem(i) for i in iso_days}
    found = False
    for day in days:
        kept: list[dict[str, Any]] = []
        for item in day.get("items") or []:
            if str(item.get("id")) == str(key) or _item_key(item) == str(key):
                found = True
                continue
            kept.append(item)
        day["items"] = kept
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
    found = False
    for day in days:
        kept = []
        for item in day.get("items") or []:
            if str(item.get("id")) == str(key) or _item_key(item) == str(key):
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
