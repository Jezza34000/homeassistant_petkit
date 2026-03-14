"""Petkit integration diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pypetkitapi import Litter
from pypetkitapi.litter_container import StateLitter

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import DOMAIN
from .data import PetkitConfigEntry

TO_REDACT = [CONF_PASSWORD, CONF_USERNAME]

_PACKAGE_STATE_LABELS = {
    0: "normal",
    1: "running_out",
    2: "used_up",
    3: "expired",
    4: "not_available",
    5: "not_installed",
}


def _ts_to_iso(ts: int | str | None) -> str | None:
    """Convert a unix timestamp (seconds) to ISO 8601 string."""
    if ts is None:
        return None
    try:
        val = int(ts)
        if val <= 0:
            return None
        return datetime.fromtimestamp(val, tz=UTC).isoformat()
    except (ValueError, OSError):
        return str(ts)


def _add_if_set(out: dict, key: str, value: Any) -> None:
    """Add *value* to *out* under *key* when it is not None."""
    if value is not None:
        out[key] = value


def _waste_bin_diag(state: StateLitter) -> dict[str, Any]:
    """集便仓 (Waste collection bin) diagnostics."""
    d: dict[str, Any] = {}
    _add_if_set(d, "box_full", state.box_full)
    _add_if_set(d, "box_state", state.box_state)
    _add_if_set(d, "box_store_state", state.box_store_state)
    _add_if_set(d, "pack_state", state.pack_state)
    _add_if_set(d, "bagging_state", state.bagging_state)
    _add_if_set(d, "box", state.box)
    return d


def _garbage_bag_box_diag(state: StateLitter, entity: Litter) -> dict[str, Any]:
    """垃圾袋盒 (Garbage bag box) diagnostics."""
    d: dict[str, Any] = {}
    _add_if_set(d, "package_sn", state.package_sn)
    if state.package_state is not None:
        d["package_state"] = state.package_state
        d["package_state_label"] = _PACKAGE_STATE_LABELS.get(
            state.package_state, f"unknown({state.package_state})"
        )
    if state.package_install is not None:
        d["package_install"] = state.package_install
        d["package_install_time"] = _ts_to_iso(state.package_install)
    _add_if_set(d, "package_used_count", entity.package_used_count)
    _add_if_set(d, "package_total_count", entity.package_total_count)
    _add_if_set(d, "package_ignore_state", entity.package_ignore_state)
    if entity.settings and entity.settings.package_standard is not None:
        d["package_standard"] = entity.settings.package_standard
    return d


def _package_info_diag(entity: Litter) -> dict[str, Any]:
    """Last pack / replacement timestamps from t6/packageInfo."""
    d: dict[str, Any] = {}
    if entity.package_info is None:
        return d
    pr = entity.package_info.package_record
    if pr is not None and pr != "-1":
        d["last_pack_timestamp"] = pr
        d["last_pack_time"] = _ts_to_iso(pr)
    pc = entity.package_info.package_changed
    if pc is not None and pc != "-1":
        d["last_replacement_timestamp"] = pc
        d["last_replacement_time"] = _ts_to_iso(pc)
    return d


def _package_list_diag(entity: Litter) -> dict[str, Any]:
    """Packing history from t6/packageList."""
    if entity.package_list is None:
        return {}
    d: dict[str, Any] = {"total": entity.package_list.total}
    if entity.package_list.items:
        records = []
        for item in entity.package_list.items[:20]:
            rec: dict[str, Any] = {}
            if item.package_time:
                rec["package_time"] = _ts_to_iso(item.package_time)
            if item.install_time:
                rec["install_time"] = _ts_to_iso(item.install_time)
            records.append(rec)
        d["recent_records"] = records
    return d


def _spray_diag(state: StateLitter) -> dict[str, Any]:
    """喷雾 (Spray / purifying liquid) diagnostics."""
    d: dict[str, Any] = {}
    _add_if_set(d, "spray_state", state.spray_state)
    _add_if_set(d, "spray_left_days", state.spray_left_days)
    _add_if_set(d, "spray_days", state.spray_days)
    if state.spray_reset_time is not None:
        d["spray_reset_time"] = _ts_to_iso(state.spray_reset_time)
    return d


def _cat_litter_diag(state: StateLitter) -> dict[str, Any]:
    """Cat litter diagnostics."""
    d: dict[str, Any] = {}
    _add_if_set(d, "sand_status", state.sand_status)
    _add_if_set(d, "sand_percent", state.sand_percent)
    _add_if_set(d, "sand_weight", state.sand_weight)
    _add_if_set(d, "sand_lack", state.sand_lack)
    _add_if_set(d, "liquid", state.liquid)
    _add_if_set(d, "liquid_lack", state.liquid_lack)
    return d


def _sand_tray_diag(state: StateLitter) -> dict[str, Any]:
    """T7 sand tray diagnostics."""
    d: dict[str, Any] = {}
    _add_if_set(d, "sand_tray_sn", state.sand_tray_sn)
    _add_if_set(d, "sand_tray_state", state.sand_tray_state)
    _add_if_set(d, "sand_tray_use_count", state.sand_tray_use_count)
    _add_if_set(d, "sand_tray_standard_count", state.sand_tray_standard_count)
    _add_if_set(d, "sand_tray_left_day", state.sand_tray_left_day)
    if state.sand_tray_install_time is not None:
        d["sand_tray_install_time"] = _ts_to_iso(state.sand_tray_install_time)
    return d


def _litter_consumables_diag(entity: Litter) -> dict[str, Any]:
    """Build consumables/accessories diagnostics for a litter box."""
    state = entity.state
    if state is None:
        return {}

    sections: list[tuple[str, dict[str, Any]]] = [
        ("waste_bin", _waste_bin_diag(state)),
        ("garbage_bag_box", _garbage_bag_box_diag(state, entity)),
        ("package_info", _package_info_diag(entity)),
        ("package_list", _package_list_diag(entity)),
        (
            "purification_n60",
            (
                {"left_days": state.purification_left_days}
                if state.purification_left_days is not None
                else {}
            ),
        ),
        (
            "deodorant_n50",
            (
                {"left_days": state.deodorant_left_days}
                if state.deodorant_left_days is not None
                else {}
            ),
        ),
        ("spray", _spray_diag(state)),
        ("cat_litter", _cat_litter_diag(state)),
        ("sand_tray", _sand_tray_diag(state)),
    ]
    return {key: val for key, val in sections if val}


def _build_device_diag(entity: Any) -> dict[str, Any]:
    """Build diagnostics dict for a single petkit device entity."""
    diag: dict[str, Any] = {}

    if hasattr(entity, "device_nfo") and entity.device_nfo:
        nfo = entity.device_nfo
        diag["device_type"] = nfo.device_type
        diag["device_id"] = nfo.device_id

    _add_if_set(diag, "sn", getattr(entity, "sn", None))
    _add_if_set(diag, "name", getattr(entity, "name", None))
    _add_if_set(diag, "firmware", getattr(entity, "firmware", None))
    _add_if_set(diag, "hardware", getattr(entity, "hardware", None))
    _add_if_set(diag, "mac", getattr(entity, "mac", None))

    # State summary
    if hasattr(entity, "state") and entity.state:
        state = entity.state
        state_diag: dict[str, Any] = {}
        _add_if_set(state_diag, "overall", getattr(state, "overall", None))
        _add_if_set(state_diag, "pim", getattr(state, "pim", None))
        _add_if_set(state_diag, "ota", getattr(state, "ota", None))
        if getattr(state, "error_msg", None):
            state_diag["error_msg"] = state.error_msg
        if getattr(state, "error_code", None):
            state_diag["error_code"] = state.error_code
        _add_if_set(state_diag, "error_level", getattr(state, "error_level", None))
        if getattr(state, "wifi", None):
            wifi_diag: dict[str, Any] = {}
            if getattr(state.wifi, "ssid", None):
                wifi_diag["ssid"] = state.wifi.ssid
            _add_if_set(wifi_diag, "rsq", getattr(state.wifi, "rsq", None))
            if wifi_diag:
                state_diag["wifi"] = wifi_diag
        if state_diag:
            diag["state"] = state_diag

    # Consumables for litter boxes
    if isinstance(entity, Litter):
        consumables = _litter_consumables_diag(entity)
        if consumables:
            diag["consumables"] = consumables

    return diag


async def async_get_device_diagnostics(
    hass: HomeAssistant, config_entry: PetkitConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a device."""
    diag: dict[str, Any] = {
        "config_entry": async_redact_data(config_entry.data, TO_REDACT),
    }

    # Find the matching petkit entity by SN from device identifiers
    client = config_entry.runtime_data.client
    target_sn: str | None = None
    for domain, identifier in device.identifiers:
        if domain == DOMAIN:
            target_sn = identifier
            break

    if target_sn and client.petkit_entities:
        for entity in client.petkit_entities.values():
            if hasattr(entity, "sn") and entity.sn == target_sn:
                diag["device"] = _build_device_diag(entity)
                break

    return diag
