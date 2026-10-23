"""Sensors: internal temperature and supply voltage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfElectricPotential, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DP_BATTERY_VOLTAGE, DP_TEMPERATURE
from .coordinator import EuhomyConfigEntry, EuhomyCoordinator
from .entity import EuhomyEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EuhomySensorDescription(SensorEntityDescription):
    """Sensor backed by one data point."""

    dp_id: int
    convert: Callable[[int], float] = float


SENSORS: tuple[EuhomySensorDescription, ...] = (
    EuhomySensorDescription(
        key="temperature",
        dp_id=DP_TEMPERATURE,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EuhomySensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        dp_id=DP_BATTERY_VOLTAGE,
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        convert=lambda millivolts: millivolts / 1000,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EuhomyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data
    async_add_entities(EuhomySensor(coordinator, desc) for desc in SENSORS)


class EuhomySensor(EuhomyEntity, SensorEntity):
    """A numeric reading from the fridge."""

    entity_description: EuhomySensorDescription

    def __init__(
        self, coordinator: EuhomyCoordinator, description: EuhomySensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        """Return the converted data point."""
        value = self.coordinator.get_dp(self.entity_description.dp_id)
        if value is None:
            return None
        return self.entity_description.convert(int(value))
