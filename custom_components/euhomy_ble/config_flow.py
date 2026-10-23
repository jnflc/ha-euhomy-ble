"""Config flow for the Euhomy BLE integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS, CONF_NAME
import voluptuous as vol

from .const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_UUID,
    DOMAIN,
    TUYA_ADV_SERVICE_UUID,
    TUYA_MANUFACTURER_ID,
)
from .tuya_ble import (
    MAX_DEVICE_ID_LENGTH,
    MAX_UUID_LENGTH,
    TuyaBLEAuthError,
    TuyaBLEClient,
    TuyaBLEConnectionError,
    TuyaBLEError,
    TuyaBLETimeoutError,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Euhomy fridge"


def _is_tuya_ble(info: BluetoothServiceInfoBleak) -> bool:
    """True for anything that advertises like a Tuya BLE device."""
    return (
        TUYA_MANUFACTURER_ID in info.manufacturer_data
        or TUYA_ADV_SERVICE_UUID in info.service_uuids
    )


def _validate_credentials(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Cheap format checks before talking to the fridge."""
    errors: dict[str, str] = {}
    local_key = user_input[CONF_LOCAL_KEY]
    device_id = user_input[CONF_DEVICE_ID]
    uuid = user_input[CONF_UUID]
    if len(local_key) < 6 or not local_key.isascii():
        errors[CONF_LOCAL_KEY] = "invalid_local_key"
    if not device_id or len(device_id) > MAX_DEVICE_ID_LENGTH or not device_id.isascii():
        errors[CONF_DEVICE_ID] = "invalid_device_id"
    if not uuid or len(uuid) > MAX_UUID_LENGTH or not uuid.isascii():
        errors[CONF_UUID] = "invalid_uuid"
    return errors


class EuhomyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a fridge by picking it from the nearby Tuya BLE devices."""

    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._candidates: dict[str, str] = {}

    # ── Pick the device ───────────────────────────────────────────────────────

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose one of the Tuya BLE devices the Bluetooth stack currently sees."""
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(self._address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_credentials()

        configured = self._async_current_ids(include_ignore=False)
        self._candidates = {
            info.address: f"{info.name or 'Tuya BLE device'} ({info.address}, {info.rssi} dBm)"
            for info in bluetooth.async_discovered_service_info(self.hass, connectable=True)
            if _is_tuya_ble(info) and info.address not in configured
        }
        if not self._candidates:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(self._candidates)}),
        )

    # ── Credentials ───────────────────────────────────────────────────────────

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the Tuya credentials and verify them against the fridge."""
        assert self._address is not None
        errors: dict[str, str] = {}
        defaults = user_input or {}

        if user_input is not None:
            data = {
                CONF_ADDRESS: self._address,
                CONF_LOCAL_KEY: user_input[CONF_LOCAL_KEY].strip(),
                CONF_DEVICE_ID: user_input[CONF_DEVICE_ID].strip(),
                CONF_UUID: user_input[CONF_UUID].strip(),
            }
            errors = _validate_credentials(data)
            if not errors and (error := await self._async_check(data)):
                errors["base"] = error
            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_NAME].strip() or DEFAULT_NAME, data=data
                )

        return self.async_show_form(
            step_id="credentials",
            data_schema=self._credentials_schema(defaults, with_name=True),
            errors=errors,
            description_placeholders={"address": self._address},
        )

    # ── Re-authentication ─────────────────────────────────────────────────────

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start re-authentication after the fridge rejected the credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for fresh Tuya credentials."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {
                CONF_ADDRESS: entry.data[CONF_ADDRESS],
                CONF_LOCAL_KEY: user_input[CONF_LOCAL_KEY].strip(),
                CONF_DEVICE_ID: user_input[CONF_DEVICE_ID].strip(),
                CONF_UUID: user_input[CONF_UUID].strip(),
            }
            errors = _validate_credentials(data)
            if not errors and (error := await self._async_check(data)):
                errors["base"] = error
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._credentials_schema(user_input or entry.data),
            errors=errors,
            description_placeholders={"address": entry.data[CONF_ADDRESS]},
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _credentials_schema(defaults: Mapping[str, Any], *, with_name: bool = False) -> vol.Schema:
        fields: dict[Any, Any] = {}
        if with_name:
            fields[vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME))] = str
        fields[vol.Required(CONF_LOCAL_KEY, default=defaults.get(CONF_LOCAL_KEY, ""))] = str
        fields[vol.Required(CONF_DEVICE_ID, default=defaults.get(CONF_DEVICE_ID, ""))] = str
        fields[vol.Required(CONF_UUID, default=defaults.get(CONF_UUID, ""))] = str
        return vol.Schema(fields)

    async def _async_check(self, data: Mapping[str, Any]) -> str | None:
        """Run the Tuya handshake once; returns an error key or None."""
        address = data[CONF_ADDRESS]

        def _ble_device():
            return bluetooth.async_ble_device_from_address(self.hass, address, connectable=True)

        ble_device = _ble_device()
        if ble_device is None:
            return "device_not_found"

        client = TuyaBLEClient(
            local_key=data[CONF_LOCAL_KEY],
            device_id=data[CONF_DEVICE_ID],
            uuid=data[CONF_UUID],
        )
        try:
            await client.connect(ble_device, lambda: _ble_device() or ble_device)
        except TuyaBLEAuthError:
            return "invalid_auth"
        except TuyaBLETimeoutError:
            return "no_response"
        except (TuyaBLEConnectionError, TuyaBLEError):
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error while checking the credentials")
            return "unknown"
        finally:
            await client.disconnect()
        return None
