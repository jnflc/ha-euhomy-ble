"""Tests for the config flow."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.euhomy_ble.const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_UUID,
    DOMAIN,
)
from custom_components.euhomy_ble.tuya_ble import (
    TuyaBLEAuthError,
    TuyaBLEConnectionError,
    TuyaBLEProtocolError,
    TuyaBLETimeoutError,
)

from .conftest import ADDRESS, DEVICE_ID, ENTRY_DATA, LOCAL_KEY, UUID, MockClient, make_service_info

CREDENTIALS = {
    CONF_NAME: "Camping fridge",
    CONF_LOCAL_KEY: LOCAL_KEY,
    CONF_DEVICE_ID: DEVICE_ID,
    CONF_UUID: UUID,
}


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def test_happy_path(hass: HomeAssistant, mock_client: MockClient) -> None:
    with patch("custom_components.euhomy_ble.async_setup_entry", return_value=True) as setup_entry:
        await _run_happy_path(hass, mock_client)
    setup_entry.assert_awaited_once()


async def _run_happy_path(hass: HomeAssistant, mock_client: MockClient) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    assert result["step_id"] == "credentials"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Camping fridge"
    assert result["data"] == ENTRY_DATA
    assert result["result"].unique_id == ADDRESS

    # The handshake was verified once and the connection released again.
    mock_client.connect.assert_awaited_once()
    mock_client.disconnect.assert_awaited_once()


async def test_credentials_are_stripped(hass: HomeAssistant, mock_client: MockClient) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**CREDENTIALS, CONF_LOCAL_KEY: f"  {LOCAL_KEY}\n", CONF_UUID: f" {UUID} "},
    )
    assert result["data"][CONF_LOCAL_KEY] == LOCAL_KEY
    assert result["data"][CONF_UUID] == UUID


async def test_only_tuya_devices_are_offered(
    hass: HomeAssistant, mock_client: MockClient, bluetooth_stack: MagicMock
) -> None:
    other = make_service_info(name="Some Speaker", address="11:22:33:44:55:66", tuya=False)
    bluetooth_stack.discovered.return_value = [other, make_service_info()]
    result = await _start(hass)
    assert list(result["data_schema"].schema[CONF_ADDRESS].container) == [ADDRESS]


async def test_no_devices_found(
    hass: HomeAssistant, mock_client: MockClient, bluetooth_stack: MagicMock
) -> None:
    bluetooth_stack.discovered.return_value = [make_service_info(name="Some Speaker", tuya=False)]
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_configured_fridge_is_not_offered_again(
    hass: HomeAssistant, mock_client: MockClient, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


@pytest.mark.parametrize(
    ("override", "field", "error"),
    [
        ({CONF_LOCAL_KEY: "abc"}, CONF_LOCAL_KEY, "invalid_local_key"),
        ({CONF_LOCAL_KEY: "schlüssel-1234567"}, CONF_LOCAL_KEY, "invalid_local_key"),
        ({CONF_DEVICE_ID: ""}, CONF_DEVICE_ID, "invalid_device_id"),
        ({CONF_DEVICE_ID: "x" * 23}, CONF_DEVICE_ID, "invalid_device_id"),
        ({CONF_UUID: "x" * 17}, CONF_UUID, "invalid_uuid"),
    ],
)
async def test_invalid_input_is_rejected_before_connecting(
    hass: HomeAssistant,
    mock_client: MockClient,
    override: dict,
    field: str,
    error: str,
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**CREDENTIALS, **override}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: error}
    mock_client.connect.assert_not_awaited()


@pytest.mark.parametrize(
    ("exception", "error"),
    [
        (TuyaBLEAuthError("rejected"), "invalid_auth"),
        (TuyaBLETimeoutError("no response"), "no_response"),
        (TuyaBLEConnectionError("busy"), "cannot_connect"),
        (TuyaBLEProtocolError("garbage"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_handshake_errors_and_retry(
    hass: HomeAssistant, mock_client: MockClient, exception: Exception, error: str
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )

    mock_client.connect.side_effect = exception
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    mock_client.disconnect.assert_awaited()

    # The user can correct the input and try again on the same form.
    mock_client.connect.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_fridge_vanishes_during_setup(
    hass: HomeAssistant, mock_client: MockClient, bluetooth_stack: MagicMock
) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    bluetooth_stack.from_address.return_value = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["errors"] == {"base": "device_not_found"}


async def test_reauth_updates_credentials(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MockClient
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_LOCAL_KEY: "0123456789abcdef", CONF_DEVICE_ID: DEVICE_ID, CONF_UUID: UUID},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_LOCAL_KEY] == "0123456789abcdef"
    assert config_entry.data[CONF_ADDRESS] == ADDRESS


async def test_reauth_wrong_credentials(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MockClient
) -> None:
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    mock_client.connect.side_effect = TuyaBLEAuthError("rejected")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_LOCAL_KEY: "0123456789abcdef", CONF_DEVICE_ID: DEVICE_ID, CONF_UUID: UUID},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert config_entry.data[CONF_LOCAL_KEY] == LOCAL_KEY
