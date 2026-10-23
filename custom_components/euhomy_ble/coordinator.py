"""Coordinator that owns the Tuya BLE connection to the fridge."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import CONF_DEVICE_ID, CONF_LOCAL_KEY, CONF_UUID, DOMAIN, UPDATE_INTERVAL
from .tuya_ble import (
    DataPoint,
    DpType,
    TuyaBLEAuthError,
    TuyaBLEClient,
    TuyaBLEConnectionError,
    TuyaBLEError,
)

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

# Fridges advertise several times per second; don't retry the connection more
# often than this when they do.
RECONNECT_COOLDOWN = 10.0

type EuhomyConfigEntry = ConfigEntry[EuhomyCoordinator]


class EuhomyCoordinator(DataUpdateCoordinator[dict[int, DataPoint]]):
    """Keep one persistent Tuya BLE connection and expose the latest data points."""

    config_entry: EuhomyConfigEntry

    def __init__(self, hass: HomeAssistant, entry: EuhomyConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_ADDRESS]}",
            update_interval=UPDATE_INTERVAL,
        )
        self.address: str = entry.data[CONF_ADDRESS]
        self.client = TuyaBLEClient(
            local_key=entry.data[CONF_LOCAL_KEY],
            device_id=entry.data[CONF_DEVICE_ID],
            uuid=entry.data[CONF_UUID],
            on_dps=self._handle_dps,
            on_disconnect=self._handle_disconnect,
            now=dt_util.now,
        )
        self._dps: dict[int, DataPoint] = {}
        self._last_reconnect_request = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @callback
    def async_start_listening(self) -> None:
        """Reconnect as soon as the fridge advertises again after a drop."""
        self.config_entry.async_on_unload(
            bluetooth.async_register_callback(
                self.hass,
                self._handle_advertisement,
                BluetoothCallbackMatcher(address=self.address, connectable=True),
                BluetoothScanningMode.ACTIVE,
            )
        )

    async def async_stop(self) -> None:
        """Close the connection."""
        await self.async_shutdown()
        await self.client.disconnect()

    # ── Updates ───────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[int, DataPoint]:
        """Connect if needed, otherwise send the keep-alive status request."""
        try:
            if self.client.connected:
                await self.client.request_status()
            else:
                await self._async_connect()
        except TuyaBLEAuthError as err:
            raise ConfigEntryAuthFailed("The fridge rejected the Tuya credentials") from err
        except TuyaBLEError as err:
            raise UpdateFailed(f"Cannot talk to the fridge: {err}") from err
        return dict(self._dps)

    def _ble_device(self) -> BLEDevice | None:
        return bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)

    async def _async_connect(self) -> None:
        ble_device = self._ble_device()
        if ble_device is None:
            raise TuyaBLEConnectionError("fridge is not in Bluetooth range")
        await self.client.connect(ble_device, lambda: self._ble_device() or ble_device)

    @callback
    def _handle_advertisement(
        self, _service_info: BluetoothServiceInfoBleak, _change: BluetoothChange
    ) -> None:
        if self.client.connected:
            return
        now = time.monotonic()
        if now - self._last_reconnect_request < RECONNECT_COOLDOWN:
            return
        self._last_reconnect_request = now
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _handle_dps(self, dps: list[DataPoint]) -> None:
        for dp in dps:
            self._dps[dp.dp_id] = dp
        self.async_set_updated_data(dict(self._dps))

    @callback
    def _handle_disconnect(self) -> None:
        _LOGGER.debug("Connection to %s lost", self.address)
        self.async_set_update_error(UpdateFailed("Bluetooth connection lost"))

    # ── Commands ──────────────────────────────────────────────────────────────

    def get_dp(self, dp_id: int) -> bool | int | str | bytes | None:
        """Latest value of a data point, or None if it was never reported."""
        dp = self.data.get(dp_id) if self.data else None
        return dp.value if dp is not None else None

    async def async_write_dp(self, dp_id: int, dp_type: DpType, value: bool | int) -> None:
        """Write one data point and reflect it optimistically."""
        dp = DataPoint(dp_id, dp_type, value)
        try:
            if not self.client.connected:
                await self._async_connect()
            await self.client.send_dps([dp])
        except TuyaBLEAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError("The fridge rejected the Tuya credentials") from err
        except TuyaBLEError as err:
            raise HomeAssistantError(f"Could not write to the fridge: {err}") from err
        self._dps[dp_id] = dp
        self.async_set_updated_data(dict(self._dps))
