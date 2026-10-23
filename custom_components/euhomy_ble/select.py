"""Selects: battery protection level and display unit."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DP_BATTERY_PROTECTION, DP_DISPLAY_UNIT
from .coordinator import EuhomyConfigEntry, EuhomyCoordinator
from .entity import EuhomyEntity
from .tuya_ble import DpType

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EuhomySelectDescription(SelectEntityDescription):
    """Select backed by one enum data point (option index == enum value)."""

    dp_id: int


SELECTS: tuple[EuhomySelectDescription, ...] = (
    EuhomySelectDescription(
        key="battery_protection",
        translation_key="battery_protection",
        dp_id=DP_BATTERY_PROTECTION,
        options=["low", "medium", "high"],
        entity_category=EntityCategory.CONFIG,
    ),
    EuhomySelectDescription(
        key="display_unit",
        translation_key="display_unit",
        dp_id=DP_DISPLAY_UNIT,
        options=["celsius", "fahrenheit"],
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EuhomyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the selects."""
    coordinator = entry.runtime_data
    async_add_entities(EuhomySelect(coordinator, desc) for desc in SELECTS)


class EuhomySelect(EuhomyEntity, SelectEntity):
    """An enum setting of the fridge."""

    entity_description: EuhomySelectDescription

    def __init__(
        self, coordinator: EuhomyCoordinator, description: EuhomySelectDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def current_option(self) -> str | None:
        """Return the active option, or None if unknown or out of range."""
        value = self.coordinator.get_dp(self.entity_description.dp_id)
        options = self.entity_description.options or []
        if isinstance(value, int) and 0 <= value < len(options):
            return options[value]
        return None

    async def async_select_option(self, option: str) -> None:
        """Write the selected option."""
        options = self.entity_description.options or []
        await self.coordinator.async_write_dp(
            self.entity_description.dp_id, DpType.ENUM, options.index(option)
        )
