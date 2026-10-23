"""Fixtures for the Euhomy BLE tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.backends.device import BLEDevice
from habluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.euhomy_ble.const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_UUID,
    DOMAIN,
)
from custom_components.euhomy_ble.tuya_ble import DataPoint, DpType

ADDRESS = "AA:BB:CC:DD:EE:FF"
LOCAL_KEY = "abcdef0123456789"
DEVICE_ID = "d4b9e7fgm2nkw3xp"
UUID = "3c7a2f5d8b4e9106"

ENTRY_DATA = {
    CONF_ADDRESS: ADDRESS,
    CONF_LOCAL_KEY: LOCAL_KEY,
    CONF_DEVICE_ID: DEVICE_ID,
    CONF_UUID: UUID,
}

# What a CF45 in standby-less normal operation might report.
INITIAL_DPS = [
    DataPoint(101, DpType.BOOL, True),
    DataPoint(102, DpType.ENUM, 0),
    DataPoint(103, DpType.ENUM, 0),
    DataPoint(104, DpType.ENUM, 1),
    DataPoint(105, DpType.ENUM, 0),
    DataPoint(112, DpType.INT, 5),
    DataPoint(114, DpType.INT, 4),
    DataPoint(117, DpType.INT, 41),
    DataPoint(119, DpType.INT, 39),
    DataPoint(122, DpType.INT, 14700),
]


def make_service_info(
    *, name: str = "TY", address: str = ADDRESS, tuya: bool = True
) -> BluetoothServiceInfoBleak:
    """Build an advertisement like the one LightBlue shows for the fridge."""
    device = BLEDevice(address, name, {})
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={0x07D0: bytes.fromhex("80030000010000")} if tuya else {},
        service_data={},
        service_uuids=["0000a201-0000-1000-8000-00805f9b34fb"] if tuya else [],
        source="local",
        device=device,
        advertisement=None,
        connectable=True,
        time=0,
        tx_power=None,
    )


class MockClient:
    """Stand-in for TuyaBLEClient that reports INITIAL_DPS on connect."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.connected = False
        self.device_info = bytes.fromhex("010203040000a1b2c3d4e5f6")
        self.connect = AsyncMock(side_effect=self._connect)
        self.disconnect = AsyncMock(side_effect=self._disconnect)
        self.request_status = AsyncMock()
        self.send_dps = AsyncMock()

    async def _connect(self, *_args: Any, **_kwargs: Any) -> None:
        self.connected = True
        if on_dps := self.kwargs.get("on_dps"):
            on_dps(INITIAL_DPS)

    async def _disconnect(self) -> None:
        self.connected = False

    def drop(self) -> None:
        """Simulate the fridge dropping the link."""
        self.connected = False
        self.kwargs["on_disconnect"]()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow the custom integration to load."""


@pytest.fixture(autouse=True)
def bluetooth_stack(hass: HomeAssistant) -> Generator[MagicMock]:
    """Pretend the Bluetooth integration is loaded and the fridge is in range."""
    hass.config.components.add("bluetooth")
    hass.config.components.add("bluetooth_adapters")
    ble_device = BLEDevice(ADDRESS, "TY", {})
    unsub = MagicMock()
    with (
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ) as from_address,
        patch(
            "homeassistant.components.bluetooth.async_register_callback",
            return_value=unsub,
        ) as register,
        patch(
            "homeassistant.components.bluetooth.async_discovered_service_info",
            return_value=[make_service_info()],
        ) as discovered,
    ):
        yield MagicMock(
            from_address=from_address,
            register=register,
            discovered=discovered,
            unsub=unsub,
            ble_device=ble_device,
        )


@pytest.fixture
def mock_client() -> Generator[MockClient]:
    """Replace the Tuya BLE client everywhere it is constructed."""
    client = MockClient()

    def factory(**kwargs: Any) -> MockClient:
        client.kwargs = kwargs
        return client

    with (
        patch("custom_components.euhomy_ble.coordinator.TuyaBLEClient", factory),
        patch("custom_components.euhomy_ble.config_flow.TuyaBLEClient", factory),
    ):
        yield client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A configured fridge."""
    return MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, title="Euhomy fridge", data=dict(ENTRY_DATA)
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MockClient
) -> MockConfigEntry:
    """Set the config entry up."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
