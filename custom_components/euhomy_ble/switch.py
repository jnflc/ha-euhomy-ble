"""Switch: lock the fridge's control panel."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DP_PANEL_LOCK
from .coordinator import EuhomyConfigEntry, EuhomyCoordinator
from .entity import EuhomyEntity
from .tuya_ble import DpType

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EuhomyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the panel lock switch."""
    async_add_entities([EuhomyPanelLock(entry.runtime_data)])


class EuhomyPanelLock(EuhomyEntity, SwitchEntity):
    """Disable the buttons on the fridge."""

    _attr_translation_key = "panel_lock"

    def __init__(self, coordinator: EuhomyCoordinator) -> None:
        super().__init__(coordinator, "panel_lock")

    @property
    def is_on(self) -> bool | None:
        """Return True while the panel is locked."""
        value = self.coordinator.get_dp(DP_PANEL_LOCK)
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Lock the panel."""
        await self.coordinator.async_write_dp(DP_PANEL_LOCK, DpType.ENUM, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Unlock the panel."""
        await self.coordinator.async_write_dp(DP_PANEL_LOCK, DpType.ENUM, 0)
