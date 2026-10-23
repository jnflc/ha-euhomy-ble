"""Climate entity: power, target temperature and ECO/MAX mode."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    DP_MODE,
    DP_POWER,
    DP_TARGET_TEMPERATURE,
    DP_TEMPERATURE,
    MAX_TEMP_C,
    MIN_TEMP_C,
    MODE_ECO,
    MODE_MAX,
    PRESET_ECO,
    PRESET_MAX,
)
from .coordinator import EuhomyConfigEntry, EuhomyCoordinator
from .entity import EuhomyEntity
from .tuya_ble import DpType

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EuhomyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the fridge climate entity."""
    async_add_entities([EuhomyClimate(entry.runtime_data)])


class EuhomyClimate(EuhomyEntity, ClimateEntity):
    """The fridge as a thermostat that can only cool."""

    _attr_name = None
    _attr_translation_key = "fridge"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL]
    _attr_preset_modes = [PRESET_MAX, PRESET_ECO]
    _attr_min_temp = MIN_TEMP_C
    _attr_max_temp = MAX_TEMP_C
    _attr_target_temperature_step = 1
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: EuhomyCoordinator) -> None:
        super().__init__(coordinator, "climate")

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return OFF while the fridge is in standby."""
        power = self.coordinator.get_dp(DP_POWER)
        if power is None:
            return None
        return HVACMode.COOL if power else HVACMode.OFF

    @property
    def current_temperature(self) -> float | None:
        """Return the temperature inside the fridge."""
        value = self.coordinator.get_dp(DP_TEMPERATURE)
        return float(value) if value is not None else None

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        value = self.coordinator.get_dp(DP_TARGET_TEMPERATURE)
        return float(value) if value is not None else None

    @property
    def preset_mode(self) -> str | None:
        """Return the cooling mode."""
        value = self.coordinator.get_dp(DP_MODE)
        if value is None:
            return None
        return PRESET_ECO if value == MODE_ECO else PRESET_MAX

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Switch the fridge on or off."""
        await self.coordinator.async_write_dp(DP_POWER, DpType.BOOL, hvac_mode != HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await self.coordinator.async_write_dp(
                DP_TARGET_TEMPERATURE, DpType.INT, round(temperature)
            )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Select MAX or ECO."""
        await self.coordinator.async_write_dp(
            DP_MODE, DpType.ENUM, MODE_ECO if preset_mode == PRESET_ECO else MODE_MAX
        )
