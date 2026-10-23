"""Diagnostics for the Euhomy BLE integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_ID, CONF_LOCAL_KEY, CONF_UUID
from .coordinator import EuhomyConfigEntry

TO_REDACT = {CONF_ADDRESS, CONF_DEVICE_ID, CONF_LOCAL_KEY, CONF_UUID, "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EuhomyConfigEntry
) -> dict[str, Any]:
    """Return diagnostics, including every data point the fridge reported.

    The raw data points are what is needed to support a new fridge model: any
    id that is not mapped in ``const.py`` shows up here.
    """
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "connected": coordinator.client.connected,
        "last_update_success": coordinator.last_update_success,
        "device_info_hex": coordinator.client.device_info.hex(),
        "data_points": {
            str(dp_id): {"type": dp.dp_type.name, "value": repr(dp.value)}
            for dp_id, dp in sorted((coordinator.data or {}).items())
        },
    }
