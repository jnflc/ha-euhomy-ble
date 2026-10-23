"""Base entity for the Euhomy BLE integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EuhomyCoordinator


class EuhomyEntity(CoordinatorEntity[EuhomyCoordinator]):
    """An entity backed by one Euhomy fridge."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EuhomyCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            manufacturer="Euhomy",
            model="Portable compressor fridge",
            name=coordinator.config_entry.title,
        )
