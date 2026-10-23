"""The Euhomy BLE integration."""

from __future__ import annotations

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant

from .coordinator import EuhomyConfigEntry, EuhomyCoordinator

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: EuhomyConfigEntry) -> bool:
    """Set up a Euhomy fridge from a config entry."""
    coordinator = EuhomyCoordinator(hass, entry)
    entry.runtime_data = coordinator

    # Raises ConfigEntryNotReady if the fridge is out of range or busy (for
    # example connected to the phone app) and ConfigEntryAuthFailed if the
    # Tuya credentials are rejected.
    await coordinator.async_config_entry_first_refresh()

    coordinator.async_start_listening()

    async def _async_close(_event: Event) -> None:
        await coordinator.async_stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_close))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EuhomyConfigEntry) -> bool:
    """Unload a config entry and release the Bluetooth connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
