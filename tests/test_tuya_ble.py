"""Tests for the Tuya BLE protocol implementation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import struct
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
import pytest

from custom_components.euhomy_ble import tuya_ble
from custom_components.euhomy_ble.tuya_ble import (
    Code,
    DataPoint,
    DpType,
    Reassembler,
    TuyaBLEAuthError,
    TuyaBLEClient,
    TuyaBLEConnectionError,
    TuyaBLEProtocolError,
    TuyaBLETimeoutError,
    TuyaBLEWriteError,
    crc16,
    decode_dps,
    decrypt_message,
    derive_login_key,
    derive_session_key,
    encode_dp,
    encrypt_message,
    pack_varint,
    split_into_packets,
    unpack_varint,
)

from .fake_device import DEVICE_ID, LOCAL_KEY, UUID, FakeDevice

BLE_DEVICE = BLEDevice("AA:BB:CC:DD:EE:FF", "TY", {})


# ── Primitives ────────────────────────────────────────────────────────────────


def test_crc16_modbus_check_value() -> None:
    assert crc16(b"123456789") == 0x4B37


@pytest.mark.parametrize("value", [0, 1, 127, 128, 300, 16383, 16384, 2**28])
def test_varint_round_trip(value: int) -> None:
    encoded = pack_varint(value)
    assert unpack_varint(encoded, 0) == (value, len(encoded))


def test_varint_known_encoding() -> None:
    assert pack_varint(300) == bytes([0xAC, 0x02])


def test_varint_truncated() -> None:
    with pytest.raises(TuyaBLEProtocolError):
        unpack_varint(b"\x80", 0)


def test_key_derivation() -> None:
    # Only the first six characters of the local key matter.
    assert derive_login_key("abcdef0123456789") == derive_login_key("abcdefXXXXXXXXXX")
    assert derive_session_key("abcdef0123", b"123456") != derive_login_key("abcdef0123")
    assert len(derive_session_key("abcdef0123", b"123456")) == 16


def test_message_round_trip() -> None:
    key = derive_login_key(LOCAL_KEY)
    payload = encrypt_message(
        key=key, security_flag=4, seq=7, response_to=3, code=0x8001, data=b"hello"
    )
    assert payload[0] == 4
    message = decrypt_message(payload, {4: key})
    assert (message.seq, message.response_to, message.code, message.data) == (
        7,
        3,
        0x8001,
        b"hello",
    )


def test_message_with_empty_data_is_block_aligned() -> None:
    key = derive_login_key(LOCAL_KEY)
    payload = encrypt_message(key=key, security_flag=4, seq=1, code=0, data=b"")
    assert (len(payload) - 17) % 16 == 0
    assert decrypt_message(payload, {4: key}).data == b""


def test_wrong_key_is_detected() -> None:
    payload = encrypt_message(
        key=derive_login_key(LOCAL_KEY), security_flag=4, seq=1, code=0, data=b"x" * 20
    )
    with pytest.raises(TuyaBLEProtocolError):
        decrypt_message(payload, {4: derive_login_key("zzzzzz")})


def test_unknown_security_flag_and_short_payload() -> None:
    key = derive_login_key(LOCAL_KEY)
    payload = encrypt_message(key=key, security_flag=1, seq=1, code=0, data=b"")
    with pytest.raises(TuyaBLEProtocolError):
        decrypt_message(payload, {4: key})
    with pytest.raises(TuyaBLEProtocolError):
        decrypt_message(b"\x04" + bytes(10), {4: key})
    with pytest.raises(TuyaBLEProtocolError):
        decrypt_message(payload[:-1], {1: key})


def test_fragmentation_round_trip() -> None:
    payload = bytes(range(200))
    packets = split_into_packets(payload)
    assert all(len(p) <= 20 for p in packets)
    assert len(packets) > 1

    reassembler = Reassembler()
    results = [reassembler.feed(p) for p in packets]
    assert results[:-1] == [None] * (len(packets) - 1)
    assert results[-1] == payload


def test_single_fragment_header() -> None:
    packets = split_into_packets(b"\x01\x02\x03")
    assert packets == [b"\x00\x03\x20\x01\x02\x03"]


def test_reassembler_rejects_out_of_order_and_recovers() -> None:
    packets = split_into_packets(bytes(100))
    reassembler = Reassembler()
    reassembler.feed(packets[0])
    with pytest.raises(TuyaBLEProtocolError):
        reassembler.feed(packets[2])
    # A fresh first fragment starts over.
    assert [reassembler.feed(p) for p in packets][-1] == bytes(100)


# ── Data points ───────────────────────────────────────────────────────────────


def test_encode_known_vectors() -> None:
    assert encode_dp(DataPoint(114, DpType.INT, 4)) == bytes.fromhex("72020400000004")
    assert encode_dp(DataPoint(114, DpType.INT, -18)) == bytes.fromhex("720204ffffffee")
    assert encode_dp(DataPoint(101, DpType.BOOL, True)) == bytes.fromhex("650101" + "01")
    assert encode_dp(DataPoint(103, DpType.ENUM, 1)) == bytes.fromhex("670401" + "01")


def test_dp_round_trip_all_types() -> None:
    dps = [
        DataPoint(1, DpType.BOOL, True),
        DataPoint(2, DpType.INT, -20),
        DataPoint(3, DpType.INT, 14700),
        DataPoint(4, DpType.ENUM, 2),
        DataPoint(5, DpType.STRING, "hi"),
        DataPoint(6, DpType.RAW, b"\x01\x02"),
        DataPoint(7, DpType.BITMAP, 0x0102),
    ]
    assert decode_dps(b"".join(encode_dp(dp) for dp in dps)) == dps


def test_decode_stops_on_truncation_and_skips_unknown_type() -> None:
    good = encode_dp(DataPoint(1, DpType.BOOL, True))
    unknown = bytes([9, 0x7F, 1, 0])
    truncated = bytes([2, DpType.INT, 4, 0, 0])
    assert decode_dps(good + unknown + good) == [
        DataPoint(1, DpType.BOOL, True),
        DataPoint(1, DpType.BOOL, True),
    ]
    assert decode_dps(good + truncated) == [DataPoint(1, DpType.BOOL, True)]


# ── Client ────────────────────────────────────────────────────────────────────


TUYA_CHARACTERISTICS = [
    (tuya_ble.NOTIFY_CHAR_UUID, ["read", "notify"]),
    (tuya_ble.WRITE_CHAR_UUID, ["read", "write", "write-without-response"]),
]


class FakeCharacteristic:
    def __init__(self, uuid: str, properties: list[str]) -> None:
        self.uuid = uuid
        self.properties = properties


class FakeService:
    def __init__(self, uuid: str, characteristics: list[FakeCharacteristic]) -> None:
        self.uuid = uuid
        self.characteristics = characteristics


class FakeServices:
    """Just enough of BleakGATTServiceCollection for resolve_characteristics."""

    def __init__(self, services: list[FakeService]) -> None:
        self._services = services

    def __iter__(self):
        return iter(self._services)

    def get_service(self, uuid: str) -> FakeService | None:
        return next((s for s in self._services if s.uuid == uuid), None)

    def get_characteristic(self, uuid: str) -> FakeCharacteristic | None:
        return next((c for s in self._services for c in s.characteristics if c.uuid == uuid), None)


class FakeBleakClient:
    """Stands in for BleakClientWithServiceCache and talks to a FakeDevice."""

    def __init__(
        self,
        device: FakeDevice,
        *,
        notify_error: bool = False,
        characteristics: list[tuple[str, list[str]]] | None = None,
        service_uuid: str = tuya_ble.SERVICE_UUID,
    ) -> None:
        self.device = device
        self.notify_error = notify_error
        self.disconnected_callback = None
        self.disconnect = AsyncMock()
        chars = characteristics if characteristics is not None else TUYA_CHARACTERISTICS
        self.services = FakeServices(
            [FakeService(service_uuid, [FakeCharacteristic(u, p) for u, p in chars])]
        )
        self.notify_uuid = next((u for u, p in chars if "notify" in p), None)
        self.write_uuid = next((u for u, p in chars if "write-without-response" in p), None)

    async def start_notify(self, uuid: str, callback) -> None:
        assert uuid == self.notify_uuid
        if self.notify_error:
            raise BleakError("characteristic not found")
        self.device.notify = callback

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = True) -> None:
        assert uuid == self.write_uuid
        assert response is False
        assert len(data) <= 20
        self.device.on_write(bytes(data))


def make_client(
    device: FakeDevice,
    *,
    notify_error: bool = False,
    characteristics: list[tuple[str, list[str]]] | None = None,
    service_uuid: str = tuya_ble.SERVICE_UUID,
    **kwargs,
):
    """Return a client whose BLE connection is routed to ``device``."""
    bleak_client = FakeBleakClient(
        device,
        notify_error=notify_error,
        characteristics=characteristics,
        service_uuid=service_uuid,
    )

    async def establish(client_class, ble_device, name, disconnected_callback, **_kw):
        bleak_client.disconnected_callback = disconnected_callback
        return bleak_client

    client = TuyaBLEClient(local_key=LOCAL_KEY, device_id=DEVICE_ID, uuid=UUID, **kwargs)
    patcher = patch.object(tuya_ble, "establish_connection", establish)
    patcher.start()
    return client, bleak_client, patcher


@pytest.fixture
def stop_patches():
    patchers = []
    yield patchers
    for patcher in patchers:
        patcher.stop()


async def test_connect_handshake_and_initial_report(stop_patches) -> None:
    device = FakeDevice(initial_dps=[DataPoint(112, DpType.INT, 5)])
    received: list[list[DataPoint]] = []
    client, _bleak, patcher = make_client(device, on_dps=received.append)
    stop_patches.append(patcher)

    await client.connect(BLE_DEVICE)

    assert client.connected
    assert [m.code for m in device.received] == [
        Code.DEVICE_INFO,
        Code.PAIR,
        Code.DEVICE_STATUS,
    ]
    assert received == [[DataPoint(112, DpType.INT, 5)]]
    assert len(client.device_info) >= 12


async def test_already_paired_is_accepted(stop_patches) -> None:
    device = FakeDevice(pair_result=2)
    client, _bleak, patcher = make_client(device)
    stop_patches.append(patcher)
    await client.connect(BLE_DEVICE)
    assert client.connected


async def test_pair_rejected_raises_auth_error(stop_patches) -> None:
    device = FakeDevice(pair_result=1)
    client, bleak, patcher = make_client(device)
    stop_patches.append(patcher)
    with pytest.raises(TuyaBLEAuthError):
        await client.connect(BLE_DEVICE)
    assert not client.connected
    bleak.disconnect.assert_awaited()


async def test_wrong_device_id_is_rejected_by_device(stop_patches) -> None:
    device = FakeDevice(device_id="someone-elses-id")
    client, _bleak, patcher = make_client(device)
    stop_patches.append(patcher)
    with pytest.raises(TuyaBLEAuthError):
        await client.connect(BLE_DEVICE)


async def test_silent_device_times_out(stop_patches) -> None:
    device = FakeDevice(answer_info=False)
    client, bleak, patcher = make_client(device)
    stop_patches.append(patcher)
    with (
        patch.object(tuya_ble, "RESPONSE_TIMEOUT", 0.05),
        pytest.raises(TuyaBLETimeoutError),
    ):
        await client.connect(BLE_DEVICE)
    assert not client.connected
    bleak.disconnect.assert_awaited()


async def test_connection_failure_is_wrapped() -> None:
    client = TuyaBLEClient(local_key=LOCAL_KEY, device_id=DEVICE_ID, uuid=UUID)
    with (
        patch.object(tuya_ble, "establish_connection", AsyncMock(side_effect=BleakError("nope"))),
        pytest.raises(TuyaBLEConnectionError),
    ):
        await client.connect(BLE_DEVICE)


async def test_unusual_characteristic_uuids_are_found_by_their_properties(stop_patches) -> None:
    """If a firmware doesn't use 0x2b10/0x2b11, the Module->Phone/Phone->Module pair is used."""
    device = FakeDevice(initial_dps=[DataPoint(112, DpType.INT, 5)])
    client, bleak, patcher = make_client(
        device,
        characteristics=[
            ("00009999-0000-1000-8000-00805f9b34fb", ["read", "notify"]),
            ("00008888-0000-1000-8000-00805f9b34fb", ["read", "write", "write-without-response"]),
        ],
    )
    stop_patches.append(patcher)

    await client.connect(BLE_DEVICE)

    assert client.connected
    assert bleak.notify_uuid == "00009999-0000-1000-8000-00805f9b34fb"
    assert bleak.write_uuid == "00008888-0000-1000-8000-00805f9b34fb"
    await client.send_dps([DataPoint(114, DpType.INT, 3)])
    assert device.written_dps == [DataPoint(114, DpType.INT, 3)]


async def test_write_only_characteristic_is_accepted_as_fallback(stop_patches) -> None:
    device = FakeDevice()
    client, bleak, patcher = make_client(device)
    stop_patches.append(patcher)
    bleak.services = FakeServices(
        [
            FakeService(
                tuya_ble.SERVICE_UUID,
                [
                    FakeCharacteristic("0000aaaa-0000-1000-8000-00805f9b34fb", ["notify"]),
                    FakeCharacteristic("0000bbbb-0000-1000-8000-00805f9b34fb", ["write"]),
                ],
            )
        ]
    )
    bleak.notify_uuid = "0000aaaa-0000-1000-8000-00805f9b34fb"
    bleak.write_uuid = "0000bbbb-0000-1000-8000-00805f9b34fb"
    assert tuya_ble.resolve_characteristics(bleak) == (bleak.notify_uuid, bleak.write_uuid)


async def test_foreign_device_without_tuya_service_is_rejected(stop_patches) -> None:
    client, bleak, patcher = make_client(
        FakeDevice(),
        service_uuid="0000180a-0000-1000-8000-00805f9b34fb",
        characteristics=[("00002a50-0000-1000-8000-00805f9b34fb", ["read"])],
    )
    stop_patches.append(patcher)
    with pytest.raises(TuyaBLEConnectionError, match="0x1910"):
        await client.connect(BLE_DEVICE)
    assert not client.connected
    bleak.disconnect.assert_awaited()


async def test_service_without_notify_characteristic_is_rejected(stop_patches) -> None:
    client, _bleak, patcher = make_client(
        FakeDevice(), characteristics=[("00001111-0000-1000-8000-00805f9b34fb", ["read", "write"])]
    )
    stop_patches.append(patcher)
    with pytest.raises(TuyaBLEConnectionError, match="notify/write"):
        await client.connect(BLE_DEVICE)


async def test_missing_characteristic_is_connection_error(stop_patches) -> None:
    client, _bleak, patcher = make_client(FakeDevice(), notify_error=True)
    stop_patches.append(patcher)
    with pytest.raises(TuyaBLEConnectionError):
        await client.connect(BLE_DEVICE)


async def test_send_dps_ack_and_rejection(stop_patches) -> None:
    device = FakeDevice()
    client, _bleak, patcher = make_client(device)
    stop_patches.append(patcher)
    await client.connect(BLE_DEVICE)

    await client.send_dps([DataPoint(114, DpType.INT, -4)])
    assert device.written_dps == [DataPoint(114, DpType.INT, -4)]

    device.write_result = 1
    with pytest.raises(TuyaBLEWriteError):
        await client.send_dps([DataPoint(101, DpType.BOOL, True)])


async def test_push_is_acknowledged_and_reported(stop_patches) -> None:
    device = FakeDevice()
    received: list[list[DataPoint]] = []
    client, _bleak, patcher = make_client(device, on_dps=received.append)
    stop_patches.append(patcher)
    await client.connect(BLE_DEVICE)

    device.push_dps([DataPoint(122, DpType.INT, 14700), DataPoint(103, DpType.ENUM, 1)])
    push_seq = device._seq
    await asyncio.sleep(0)  # let the acknowledgement task run

    assert received == [[DataPoint(122, DpType.INT, 14700), DataPoint(103, DpType.ENUM, 1)]]
    ack = device.replies[Code.RECEIVE_DP]
    assert ack.data == b""
    assert ack.response_to == push_seq


async def test_time_requests_are_answered(stop_patches) -> None:
    fixed = datetime(2026, 10, 23, 23, 20, 5, tzinfo=timezone(timedelta(hours=2)))
    device = FakeDevice()
    client, _bleak, patcher = make_client(device, now=lambda: fixed)
    stop_patches.append(patcher)
    await client.connect(BLE_DEVICE)

    device.request(Code.TIME1_REQUEST)
    device.request(Code.TIME2_REQUEST)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    time1 = device.replies[Code.TIME1_REQUEST].data
    assert time1[:13] == str(int(fixed.timestamp() * 1000)).encode()
    assert struct.unpack(">h", time1[13:]) == (200,)
    assert device.replies[Code.TIME2_REQUEST].data == struct.pack(
        ">BBBBBBBh", 26, 10, 23, 23, 20, 5, fixed.weekday(), 200
    )


async def test_disconnect_callback_only_for_unexpected_drops(stop_patches) -> None:
    device = FakeDevice()
    on_disconnect = MagicMock()
    client, bleak, patcher = make_client(device, on_disconnect=on_disconnect)
    stop_patches.append(patcher)
    await client.connect(BLE_DEVICE)

    bleak.disconnected_callback(bleak)
    on_disconnect.assert_called_once()
    assert not client.connected

    on_disconnect.reset_mock()
    await client.connect(BLE_DEVICE)
    await client.disconnect()
    bleak.disconnected_callback(bleak)
    on_disconnect.assert_not_called()


async def test_status_request_without_ack_is_tolerated(stop_patches) -> None:
    device = FakeDevice()
    client, _bleak, patcher = make_client(device)
    stop_patches.append(patcher)
    await client.connect(BLE_DEVICE)

    device.answer_status = False
    with patch.object(tuya_ble, "RESPONSE_TIMEOUT", 0.05):
        await client.request_status()  # must not raise


async def test_garbage_notifications_are_dropped_without_breaking_the_link(stop_patches) -> None:
    device = FakeDevice()
    received: list[list[DataPoint]] = []
    client, _bleak, patcher = make_client(device, on_dps=received.append)
    stop_patches.append(patcher)
    await client.connect(BLE_DEVICE)

    # Random bytes, a truncated varint and a fragment out of sequence.
    for garbage in (bytes(range(40)), b"\x80", b"\x05" + bytes(19)):
        device.notify(None, bytearray(garbage))

    # The link still works afterwards.
    device.push_dps([DataPoint(112, DpType.INT, 6)])
    assert received == [[DataPoint(112, DpType.INT, 6)]]
    assert client.connected
