"""Real client and real coordinator against a simulated fridge (no mocks in between)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.euhomy_ble import tuya_ble
from custom_components.euhomy_ble.tuya_ble import DataPoint, DpType

from .conftest import DEVICE_ID, INITIAL_DPS, LOCAL_KEY, UUID
from .fake_device import FakeDevice
from .test_tuya_ble import FakeBleakClient

CLIMATE = "climate.euhomy_fridge"


async def test_full_stack_against_simulated_fridge(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    fridge = FakeDevice(
        local_key=LOCAL_KEY, device_id=DEVICE_ID, uuid=UUID, initial_dps=INITIAL_DPS
    )
    bleak_client = FakeBleakClient(fridge)

    async def establish(_cls, _device, _name, disconnected_callback, **_kwargs):
        bleak_client.disconnected_callback = disconnected_callback
        return bleak_client

    with patch.object(tuya_ble, "establish_connection", establish):
        config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        # Handshake and initial report came through the real protocol stack.
        state = hass.states.get(CLIMATE)
        assert state.state == HVACMode.COOL
        assert state.attributes["current_temperature"] == 5
        assert state.attributes["temperature"] == 4
        assert hass.states.get("sensor.euhomy_fridge_battery_voltage").state == "14.7"

        # Commands are encoded, encrypted, fragmented and understood by the fridge.
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: CLIMATE, ATTR_TEMPERATURE: -4},
            blocking=True,
        )
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {ATTR_ENTITY_ID: CLIMATE, "preset_mode": "eco"},
            blocking=True,
        )
        assert fridge.written_dps == [
            DataPoint(114, DpType.INT, -4),
            DataPoint(103, DpType.ENUM, 1),
        ]
        assert hass.states.get(CLIMATE).attributes["temperature"] == -4

        # A report pushed by the fridge itself (the buttons on the panel were used).
        fridge.push_dps([DataPoint(114, DpType.INT, 2), DataPoint(112, DpType.INT, 3)])
        await hass.async_block_till_done()
        state = hass.states.get(CLIMATE)
        assert state.attributes["temperature"] == 2
        assert state.attributes["current_temperature"] == 3

        # Dropping the link makes everything unavailable...
        bleak_client.disconnected_callback(bleak_client)
        await hass.async_block_till_done()
        assert hass.states.get(CLIMATE).state == STATE_UNAVAILABLE

        # ...and unloading is clean.
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        bleak_client.disconnect.assert_awaited()


async def test_wrong_local_key_never_becomes_available(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """A fridge that can't read our DEVICE_INFO (wrong key) stays silent: setup retries."""
    fridge = FakeDevice(local_key="zzzzzz0123456789", answer_info=False)
    bleak_client = FakeBleakClient(fridge)
    # A real fridge silently ignores a request it cannot decrypt.
    fridge.on_write = lambda _packet: None

    with (
        patch.object(
            tuya_ble,
            "establish_connection",
            AsyncMock(return_value=bleak_client),
        ),
        patch.object(tuya_ble, "RESPONSE_TIMEOUT", 0.05),
    ):
        config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
