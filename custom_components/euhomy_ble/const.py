"""Constants for the Euhomy BLE integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "euhomy_ble"

CONF_LOCAL_KEY = "local_key"
CONF_DEVICE_ID = "device_id"
CONF_UUID = "uuid"

# Tuya BLE devices advertise manufacturer data under company id 0x07D0 and the
# 16-bit service 0xA201. The default local name is the generic "TY".
TUYA_MANUFACTURER_ID = 0x07D0
TUYA_ADV_SERVICE_UUID = "0000a201-0000-1000-8000-00805f9b34fb"

# While connected the fridge pushes data point changes on its own; this
# interval only drives the keep-alive status request and reconnect attempts.
UPDATE_INTERVAL = timedelta(seconds=30)

# ── Data points ───────────────────────────────────────────────────────────────
# Mapped by lyntoo/ha-euhomy-ble on a Euhomy CFC-25 by pressing the buttons on
# the fridge and watching the reports; they are the same Tuya product family.
DP_POWER = 101  # bool
DP_PANEL_LOCK = 102  # enum: 0 = unlocked, 1 = locked
DP_MODE = 103  # enum: 0 = MAX, 1 = ECO
DP_BATTERY_PROTECTION = 104  # enum: 0 = low, 1 = medium, 2 = high
DP_DISPLAY_UNIT = 105  # enum: 0 = °C, 1 = °F
DP_TEMPERATURE = 112  # int, °C (117 mirrors it in °F)
DP_TARGET_TEMPERATURE = 114  # int, °C (119 mirrors it in °F)
DP_BATTERY_VOLTAGE = 122  # int, mV

MODE_MAX = 0
MODE_ECO = 1

PRESET_MAX = "max"
PRESET_ECO = "eco"

# Specified range of the fridge (-4 °F … 68 °F).
MIN_TEMP_C = -20
MAX_TEMP_C = 20
