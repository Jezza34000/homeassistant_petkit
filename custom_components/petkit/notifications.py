"""HA persistent notification manager for Petkit Smart Devices integration.

Fires a native Home Assistant persistent notification whenever a relevant
device event occurs (litter-box cleaning result, waste bin full, low food,
low water, etc.). HA notification categories are managed independently from
the Petkit app/device notification switches so each endpoint can be enabled
or silenced without affecting the other.

When a binary alert clears (e.g. the waste bin has been emptied) the
matching persistent notification is automatically dismissed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pypetkitapi import D3, D4S, D4SH, W7H, Feeder, Litter, WaterFountain

from homeassistant.components.persistent_notification import async_create, async_dismiss
from homeassistant.helpers import translation

from .const import (
    NOTIFICATION_CAT_FEEDER_ERROR,
    NOTIFICATION_CAT_FEEDER_FOOD_LOW,
    NOTIFICATION_CAT_FOUNTAIN_CWT_EMPTY,
    NOTIFICATION_CAT_FOUNTAIN_CWT_LOW,
    NOTIFICATION_CAT_FOUNTAIN_FILTER,
    NOTIFICATION_CAT_FOUNTAIN_WATER_LOW,
    NOTIFICATION_CAT_FOUNTAIN_WT_FULL,
    NOTIFICATION_CAT_LITTER_BOX_FULL,
    NOTIFICATION_CAT_LITTER_ERROR,
    NOTIFICATION_CAT_LITTER_EVENT,
    NOTIFICATION_CAT_LITTER_SAND_LOW,
    NOTIFICATION_CATEGORIES,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import PetkitDataUpdateCoordinator

LOGGER = logging.getLogger(__name__)

# Translation key prefix for litter last-event states (entity.sensor.litter_last_event.state.*)
_LITTER_EVENT_TRANS_PREFIX = "component.petkit.entity.sensor.litter_last_event.state."


def _safe_get(obj: Any, *attrs: str, default: Any = None) -> Any:
    """Safely retrieve a chain of attributes without raising AttributeError."""
    try:
        for attr in attrs:
            obj = getattr(obj, attr)
    except AttributeError:
        return default
    else:
        return obj


def _device_name(device: Any) -> str:
    """Return a human-readable device name."""
    return (
        _safe_get(device, "device_nfo", "device_name")
        or _safe_get(device, "name")
        or f"PetKit device {device.id}"
    )


class PetkitNotificationManager:
    """Fires HA persistent notifications when PetKit device events occur.

    Instantiate once per config entry and pass in the main data coordinator.
    Call :meth:`async_start` after instantiation (from an async context) to
    load HA translations.  The manager then registers itself as a coordinator
    listener so it is called automatically after every data refresh.  Call
    :meth:`stop` when the config entry is unloaded to remove the listener.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: PetkitDataUpdateCoordinator,
        enabled_categories: list[str] | None = None,
    ) -> None:
        """Set up the notification manager (call async_start afterwards).

        ``enabled_categories`` restricts which notification categories are
        allowed to fire a persistent notification.  A value of ``None`` means
        all categories are enabled (backward compatible).
        """
        self.hass = hass
        self._coordinator = coordinator
        self._prev_litter_events: dict[int, str | None] = {}
        # Uses Optional[bool] as sentinel: None = not yet seen (first pass seeds
        # state without firing), True/False = known previous state.
        self._prev_binary: dict[str, bool | None] = {}
        # Loaded by async_start(); keys are full HA translation paths.
        self._translations: dict[str, str] = {}
        self._enabled_categories: frozenset[str] = frozenset(
            enabled_categories
            if enabled_categories is not None
            else NOTIFICATION_CATEGORIES
        )
        self._remove_listener = coordinator.async_add_listener(
            self._handle_coordinator_update
        )

    async def async_start(self) -> None:
        """Load HA translations and dismiss notifications for disabled categories."""
        try:
            self._translations = await translation.async_get_translations(
                self.hass,
                self.hass.config.language,
                "entity",
                integrations=["petkit"],
            )
        except Exception:
            LOGGER.exception("PetKit: failed to load translations for notifications")

        # Dismiss any previously-shown persistent notifications for categories
        # the user has just disabled.  Safe to call even if the notification
        # doesn't exist (HA treats it as a no-op).
        self._dismiss_disabled_categories()

    def _dismiss_disabled_categories(self) -> None:
        """Clean up lingering notifications for categories that are now disabled."""
        if not self._coordinator.data:
            return

        # Map: category -> list of notification-id suffixes that it controls.
        category_to_suffixes: dict[str, tuple[str, ...]] = {
            NOTIFICATION_CAT_LITTER_EVENT: ("litter_event",),
            NOTIFICATION_CAT_LITTER_BOX_FULL: ("box_full",),
            NOTIFICATION_CAT_LITTER_SAND_LOW: ("sand_lack",),
            NOTIFICATION_CAT_LITTER_ERROR: ("error",),
            NOTIFICATION_CAT_FEEDER_FOOD_LOW: ("food_low",),
            NOTIFICATION_CAT_FEEDER_ERROR: ("error",),
            NOTIFICATION_CAT_FOUNTAIN_WATER_LOW: ("lack_warning",),
            NOTIFICATION_CAT_FOUNTAIN_CWT_LOW: ("clean_water_tank_low",),
            NOTIFICATION_CAT_FOUNTAIN_CWT_EMPTY: ("clean_water_tank_empty",),
            NOTIFICATION_CAT_FOUNTAIN_WT_FULL: ("waste_tank_full",),
            NOTIFICATION_CAT_FOUNTAIN_FILTER: ("filter_warning",),
        }

        device_type_categories: dict[type, set[str]] = {
            Litter: {
                NOTIFICATION_CAT_LITTER_EVENT,
                NOTIFICATION_CAT_LITTER_BOX_FULL,
                NOTIFICATION_CAT_LITTER_SAND_LOW,
                NOTIFICATION_CAT_LITTER_ERROR,
            },
            Feeder: {
                NOTIFICATION_CAT_FEEDER_FOOD_LOW,
                NOTIFICATION_CAT_FEEDER_ERROR,
            },
            WaterFountain: {
                NOTIFICATION_CAT_FOUNTAIN_WATER_LOW,
                NOTIFICATION_CAT_FOUNTAIN_CWT_LOW,
                NOTIFICATION_CAT_FOUNTAIN_CWT_EMPTY,
                NOTIFICATION_CAT_FOUNTAIN_WT_FULL,
                NOTIFICATION_CAT_FOUNTAIN_FILTER,
            },
        }

        for device in self._coordinator.data.values():
            for device_type, categories in device_type_categories.items():
                if not isinstance(device, device_type):
                    continue
                for category in categories - self._enabled_categories:
                    for suffix in category_to_suffixes[category]:
                        self._dismiss(f"petkit_{device.id}_{suffix}")

    def stop(self) -> None:
        """Unregister the coordinator listener (call on integration unload)."""
        self._remove_listener()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _notify(self, notification_id: str, title: str, message: str) -> None:
        async_create(
            self.hass,
            message=message,
            title=title,
            notification_id=notification_id,
        )

    def _dismiss(self, notification_id: str) -> None:
        async_dismiss(self.hass, notification_id)

    def _category_enabled(self, category: str) -> bool:
        """Return True if the user has enabled the given notification category."""
        return category in self._enabled_categories

    def _translate_litter_event(self, key: str) -> str:
        """Return the translated label for a litter event key.

        Uses HA's loaded translations (user language) and falls back to the
        raw key when no translation is found.
        """
        return self._translations.get(f"{_LITTER_EVENT_TRANS_PREFIX}{key}", key)

    def _track_binary(
        self, device_id: int, key: str, current: Any
    ) -> tuple[bool, bool]:
        """Track a boolean state across updates.

        Returns ``(rose, fell)`` where *rose* means a False→True transition
        occurred and *fell* means a True→False transition occurred.

        On the first call for a given key the state is seeded without
        reporting any transition, preventing spurious notifications on
        HA start or integration reload.
        """
        state_key = f"{device_id}_{key}"
        prev = self._prev_binary.get(state_key)
        current_bool = bool(current)
        self._prev_binary[state_key] = current_bool
        # First observation — seed without transition
        if prev is None:
            return False, False
        return current_bool and not prev, not current_bool and prev

    # ------------------------------------------------------------------
    # Per-device-type checks
    # ------------------------------------------------------------------

    def _check_litter(self, device: Litter) -> None:
        """Check litter box work events and binary alerts."""
        from .utils import map_litter_event

        # --- Litter box event (cleaning completed / failed, pet visit, etc.) ---
        current_event = map_litter_event(device.device_records)
        if device.id not in self._prev_litter_events:
            self._prev_litter_events[device.id] = current_event
        else:
            prev_event = self._prev_litter_events[device.id]
            if current_event and current_event != prev_event:
                self._prev_litter_events[device.id] = current_event
                if self._category_enabled(NOTIFICATION_CAT_LITTER_EVENT):
                    label = self._translate_litter_event(current_event)
                    self._notify(
                        f"petkit_{device.id}_litter_event",
                        f"PetKit — {_device_name(device)}",
                        label,
                    )
            elif current_event != prev_event:
                self._prev_litter_events[device.id] = current_event

        # --- Waste bin full ---
        rose, fell = self._track_binary(
            device.id, "box_full", _safe_get(device, "state", "box_full")
        )
        if rose and self._category_enabled(NOTIFICATION_CAT_LITTER_BOX_FULL):
            self._notify(
                f"petkit_{device.id}_box_full",
                f"PetKit — {_device_name(device)}",
                "The waste bin is full and needs to be emptied.",
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_box_full")

        # --- Sand / litter level low ---
        rose, fell = self._track_binary(
            device.id, "sand_lack", _safe_get(device, "state", "sand_lack")
        )
        if rose and self._category_enabled(NOTIFICATION_CAT_LITTER_SAND_LOW):
            self._notify(
                f"petkit_{device.id}_sand_lack",
                f"PetKit — {_device_name(device)}",
                "Litter level is low. Please refill.",
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_sand_lack")

        # --- Device error ---
        error_msg = _safe_get(device, "state", "error_msg")
        rose, fell = self._track_binary(
            device.id, "error", error_msg is not None and bool(error_msg)
        )
        if rose and self._category_enabled(NOTIFICATION_CAT_LITTER_ERROR):
            self._notify(
                f"petkit_{device.id}_error",
                f"PetKit — {_device_name(device)}",
                str(error_msg),
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_error")

    def _check_feeder(self, device: Feeder) -> None:
        """Check feeder food-level alerts.

        Uses the same per-model thresholds as the food_level binary sensors:
        - D4S / D4SH (dual hopper): alert when hopper 1 *or* hopper 2 is empty
        - D3: alert when food level < 2 (levels 0 or 1)
        - All other feeders: alert when food level == 0
        """
        device_type = _safe_get(device, "device_nfo", "device_type", default="")
        if device_type in (D4S, D4SH):
            food1 = _safe_get(device, "state", "food1")
            food2 = _safe_get(device, "state", "food2")
            food_low = (food1 is not None and food1 == 0) or (
                food2 is not None and food2 == 0
            )
        elif device_type == D3:
            food_state = _safe_get(device, "state", "food")
            food_low = food_state is not None and food_state < 2
        else:
            food_state = _safe_get(device, "state", "food")
            food_low = food_state is not None and food_state == 0
        rose, fell = self._track_binary(device.id, "food_low", food_low)
        if rose and self._category_enabled(NOTIFICATION_CAT_FEEDER_FOOD_LOW):
            self._notify(
                f"petkit_{device.id}_food_low",
                f"PetKit — {_device_name(device)}",
                "Food level is low. Please refill.",
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_food_low")

        # --- Device error ---
        error_msg = _safe_get(device, "state", "error_msg")
        rose, fell = self._track_binary(
            device.id, "error", error_msg is not None and bool(error_msg)
        )
        if rose and self._category_enabled(NOTIFICATION_CAT_FEEDER_ERROR):
            self._notify(
                f"petkit_{device.id}_error",
                f"PetKit — {_device_name(device)}",
                str(error_msg),
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_error")

    def _check_fountain(self, device: WaterFountain) -> None:
        """Check water fountain alerts."""
        device_type = _safe_get(device, "device_nfo", "device_type", default="")
        if device_type == W7H:
            self._check_fountain_w7h(device)
        else:
            self._check_fountain_legacy(device)

    def _check_fountain_legacy(self, device: WaterFountain) -> None:
        """Check legacy water fountain alerts (CTW, etc.)."""
        # --- Water level low ---
        rose, fell = self._track_binary(
            device.id, "lack_warning", _safe_get(device, "lack_warning")
        )
        if rose and self._category_enabled(NOTIFICATION_CAT_FOUNTAIN_WATER_LOW):
            self._notify(
                f"petkit_{device.id}_lack_warning",
                f"PetKit — {_device_name(device)}",
                "Water level is low. Please refill.",
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_lack_warning")

        self._check_fountain_filter(device)

    def _check_fountain_w7h(self, device: WaterFountain) -> None:
        """Check W7H water fountain panel-light alerts."""
        cwt_state = _safe_get(device, "state", "cwt_state")
        rose, fell = self._track_binary(
            device.id, "clean_water_tank_low", cwt_state == 3
        )
        if rose and self._category_enabled(NOTIFICATION_CAT_FOUNTAIN_CWT_LOW):
            self._notify(
                f"petkit_{device.id}_clean_water_tank_low",
                f"PetKit — {_device_name(device)}",
                "Clean water tank is low. Please refill.",
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_clean_water_tank_low")

        rose, fell = self._track_binary(
            device.id, "clean_water_tank_empty", cwt_state == 2
        )
        if rose and self._category_enabled(NOTIFICATION_CAT_FOUNTAIN_CWT_EMPTY):
            self._notify(
                f"petkit_{device.id}_clean_water_tank_empty",
                f"PetKit — {_device_name(device)}",
                "Clean water tank is empty. Please refill.",
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_clean_water_tank_empty")

        wt_state = _safe_get(device, "state", "wt_state")
        rose, fell = self._track_binary(device.id, "waste_tank_full", wt_state == 2)
        if rose and self._category_enabled(NOTIFICATION_CAT_FOUNTAIN_WT_FULL):
            self._notify(
                f"petkit_{device.id}_waste_tank_full",
                f"PetKit — {_device_name(device)}",
                "Waste tank is full. Please empty.",
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_waste_tank_full")

        self._check_fountain_filter(device)

    def _check_fountain_filter(self, device: WaterFountain) -> None:
        """Check water fountain filter replacement alert."""
        rose, fell = self._track_binary(
            device.id, "filter_warning", _safe_get(device, "filter_warning")
        )
        if rose and self._category_enabled(NOTIFICATION_CAT_FOUNTAIN_FILTER):
            self._notify(
                f"petkit_{device.id}_filter_warning",
                f"PetKit — {_device_name(device)}",
                "The water filter needs to be replaced.",
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_filter_warning")

    # ------------------------------------------------------------------
    # Coordinator update handler
    # ------------------------------------------------------------------

    def _handle_coordinator_update(self) -> None:
        """Called by the coordinator after every successful data refresh."""
        if not self._coordinator.data:
            return
        for device in self._coordinator.data.values():
            try:
                if isinstance(device, Litter):
                    self._check_litter(device)
                elif isinstance(device, Feeder):
                    self._check_feeder(device)
                elif isinstance(device, WaterFountain):
                    self._check_fountain(device)
            except Exception:
                LOGGER.exception(
                    "PetKit: error while processing notification for device %s",
                    getattr(device, "id", "?"),
                )
