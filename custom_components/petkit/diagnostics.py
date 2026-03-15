"""Petkit integration diagnostics."""

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import DOMAIN
from .data import PetkitConfigEntry

TO_REDACT = [CONF_PASSWORD, CONF_USERNAME]


async def async_get_device_diagnostics(
    hass: HomeAssistant, config_entry: PetkitConfigEntry, device: DeviceEntry
) -> dict[str, any]:
    """Return diagnostics for a config entry."""

    diag: dict[str, any] = {
        "config_entry": async_redact_data(config_entry.data, TO_REDACT),
    }

    client = config_entry.runtime_data.client
    for _domain, identifier in device.identifiers:
        if _domain == DOMAIN:
            for entity in client.petkit_entities.values():
                if hasattr(entity, "sn") and entity.sn == identifier:
                    diag["device"] = entity.model_dump(mode="json")
                    break
            break

    return diag
