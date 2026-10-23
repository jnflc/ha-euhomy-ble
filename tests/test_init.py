"""Tests for setup, entities, commands and recovery."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.components.select import (
    ATTR_OPTION,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.euhomy_ble.tuya_ble import (
    DataPoint,
    DpType,
    TuyaBLEAuthError,
    TuyaBLEConnectionError,
    TuyaBLEWriteError,
)

from .conftest import ADDRESS, MockClient, make_service_info

CLIMATE = "climate.euhomy_fridge"


async def test_entities_reflect_initial_report(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    state = hass.states.get(CLIMATE)
    assert state is not None
    assert state.state == HVACMode.COOL
    assert state.attributes["current_temperature"] == 5
    assert state.attributes["temperature"] == 4
    assert state.attributes["preset_mode"] == "max"
    assert state.attributes["min_temp"] == -20
    assert state.attributes["max_temp"] == 20

    assert hass.states.get("sensor.euhomy_fridge_temperature").state == "5.0"
    assert hass.states.get("sensor.euhomy_fridge_battery_voltage").state == "14.7"
    assert hass.states.get("select.euhomy_fridge_battery_protection").state == "medium"
    assert hass.states.get("select.euhomy_fridge_display_unit").state == "celsius"
    assert hass.states.get("switch.euhomy_fridge_panel_lock").state == STATE_OFF


async def test_pushed_updates_change_state(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MockClient
) -> None:
    mock_client.kwargs["on_dps"](
        [
            DataPoint(101, DpType.BOOL, False),
            DataPoint(103, DpType.ENUM, 1),
            DataPoint(112, DpType.INT, -3),
            DataPoint(102, DpType.ENUM, 1),
            DataPoint(104, DpType.ENUM, 2),
            DataPoint(105, DpType.ENUM, 1),
        ]
    )
    await hass.async_block_till_done()

    state = hass.states.get(CLIMATE)
    assert state.state == HVACMode.OFF
    assert state.attributes["preset_mode"] == "eco"
    assert state.attributes["current_temperature"] == -3
    assert hass.states.get("switch.euhomy_fridge_panel_lock").state == STATE_ON
    assert hass.states.get("select.euhomy_fridge_battery_protection").state == "high"
    assert hass.states.get("select.euhomy_fridge_display_unit").state == "fahrenheit"


async def test_unknown_enum_value_is_unknown_not_a_crash(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MockClient
) -> None:
    mock_client.kwargs["on_dps"]([DataPoint(104, DpType.ENUM, 9)])
    await hass.async_block_till_done()
    assert hass.states.get("select.euhomy_fridge_battery_protection").state == "unknown"


@pytest.mark.parametrize(
    ("domain", "service", "data", "expected"),
    [
        (
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_TEMPERATURE: 6.0},
            DataPoint(114, DpType.INT, 6),
        ),
        (
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_TEMPERATURE: -18},
            DataPoint(114, DpType.INT, -18),
        ),
        (
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_HVAC_MODE: HVACMode.OFF},
            DataPoint(101, DpType.BOOL, False),
        ),
        (
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_HVAC_MODE: HVACMode.COOL},
            DataPoint(101, DpType.BOOL, True),
        ),
        (
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {ATTR_PRESET_MODE: "eco"},
            DataPoint(103, DpType.ENUM, 1),
        ),
        (
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {ATTR_PRESET_MODE: "max"},
            DataPoint(103, DpType.ENUM, 0),
        ),
        (CLIMATE_DOMAIN, SERVICE_TURN_OFF, {}, DataPoint(101, DpType.BOOL, False)),
        (CLIMATE_DOMAIN, SERVICE_TURN_ON, {}, DataPoint(101, DpType.BOOL, True)),
    ],
)
async def test_climate_commands(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MockClient,
    domain: str,
    service: str,
    data: dict,
    expected: DataPoint,
) -> None:
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: CLIMATE, **data}, blocking=True
    )
    mock_client.send_dps.assert_awaited_once_with([expected])


@pytest.mark.parametrize(
    ("entity", "option", "expected"),
    [
        ("select.euhomy_fridge_battery_protection", "low", DataPoint(104, DpType.ENUM, 0)),
        ("select.euhomy_fridge_battery_protection", "high", DataPoint(104, DpType.ENUM, 2)),
        ("select.euhomy_fridge_display_unit", "fahrenheit", DataPoint(105, DpType.ENUM, 1)),
    ],
)
async def test_select_commands(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MockClient,
    entity: str,
    option: str,
    expected: DataPoint,
) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity, ATTR_OPTION: option},
        blocking=True,
    )
    mock_client.send_dps.assert_awaited_once_with([expected])


async def test_panel_lock_commands(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MockClient
) -> None:
    entity = "switch.euhomy_fridge_panel_lock"
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity}, blocking=True
    )
    mock_client.send_dps.assert_awaited_with([DataPoint(102, DpType.ENUM, 1)])
    # The write is reflected immediately, before the fridge confirms it.
    assert hass.states.get(entity).state == STATE_ON

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity}, blocking=True
    )
    mock_client.send_dps.assert_awaited_with([DataPoint(102, DpType.ENUM, 0)])
    assert hass.states.get(entity).state == STATE_OFF


async def test_rejected_write_raises_and_keeps_state(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MockClient
) -> None:
    mock_client.send_dps.side_effect = TuyaBLEWriteError("device rejected write")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: CLIMATE, ATTR_TEMPERATURE: 8},
            blocking=True,
        )
    assert hass.states.get(CLIMATE).attributes["temperature"] == 4


async def test_commands_to_a_disconnected_fridge_are_ignored_by_ha(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MockClient
) -> None:
    """Unavailable entities don't receive service calls, so nothing is sent blind."""
    mock_client.drop()
    await hass.async_block_till_done()
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: CLIMATE, ATTR_TEMPERATURE: 7},
        blocking=True,
    )
    mock_client.send_dps.assert_not_awaited()


async def test_write_reconnects_if_the_link_dropped_in_the_meantime(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MockClient
) -> None:
    """Defensive path: a write racing a disconnect reconnects before sending."""
    coordinator = init_integration.runtime_data
    mock_client.drop()
    mock_client.connect.reset_mock()

    await coordinator.async_write_dp(114, DpType.INT, 7)

    mock_client.connect.assert_awaited_once()
    mock_client.send_dps.assert_awaited_once_with([DataPoint(114, DpType.INT, 7)])


async def test_write_with_rejected_credentials_starts_reauth(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MockClient
) -> None:
    mock_client.send_dps.side_effect = TuyaBLEAuthError("rejected")
    mock_client.connected = False
    mock_client.connect.side_effect = TuyaBLEAuthError("rejected")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: CLIMATE, ATTR_TEMPERATURE: 7},
            blocking=True,
        )
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler("euhomy_ble")
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_connection_loss_and_recovery_on_advertisement(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MockClient,
    bluetooth_stack: MagicMock,
) -> None:
    assert hass.states.get(CLIMATE).state == HVACMode.COOL

    mock_client.drop()
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.euhomy_fridge_temperature").state == STATE_UNAVAILABLE

    # The fridge is heard again: Home Assistant reconnects without waiting for the poll.
    advertisement_callback = bluetooth_stack.register.call_args.args[1]
    advertisement_callback(make_service_info(), None)
    await hass.async_block_till_done()

    assert mock_client.connected
    assert hass.states.get(CLIMATE).state == HVACMode.COOL


async def test_registers_for_the_fridges_advertisements(
    hass: HomeAssistant, init_integration: MockConfigEntry, bluetooth_stack: MagicMock
) -> None:
    matcher = bluetooth_stack.register.call_args.args[2]
    assert matcher["address"] == ADDRESS
    assert matcher["connectable"] is True


async def test_fridge_out_of_range_retries_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MockClient,
    bluetooth_stack: MagicMock,
) -> None:
    bluetooth_stack.from_address.return_value = None
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_busy_fridge_retries_setup(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MockClient
) -> None:
    mock_client.connect.side_effect = TuyaBLEConnectionError("phone app is connected")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_rejected_credentials_start_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MockClient
) -> None:
    mock_client.connect.side_effect = TuyaBLEAuthError("pairing rejected")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress_by_handler("euhomy_ble")
    )


async def test_unload_releases_the_connection(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MockClient,
    bluetooth_stack: MagicMock,
) -> None:
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.NOT_LOADED
    mock_client.disconnect.assert_awaited()
    bluetooth_stack.unsub.assert_called_once()


async def test_diagnostics_redact_secrets_and_list_datapoints(
    hass: HomeAssistant, hass_client, init_integration: MockConfigEntry
) -> None:
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    data = diagnostics["entry"]["data"]
    assert data["local_key"] == "**REDACTED**"
    assert data["device_id"] == "**REDACTED**"
    assert data["uuid"] == "**REDACTED**"
    assert data["address"] == "**REDACTED**"
    assert diagnostics["entry"]["unique_id"] == "**REDACTED**"
    assert diagnostics["connected"] is True
    assert diagnostics["data_points"]["114"] == {"type": "INT", "value": "4"}
    assert diagnostics["data_points"]["117"] == {"type": "INT", "value": "41"}
    assert "abcdef0123456789" not in str(diagnostics)
