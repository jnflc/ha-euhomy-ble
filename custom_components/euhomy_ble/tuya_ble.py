"""Minimal Tuya BLE (protocol v3) client.

Euhomy fridges expose the stock Tuya BLE GATT profile (service 0x1910 with a
"Module->Phone" notify and a "Phone->Module" write characteristic). The wire
protocol was worked out by the Tuya BLE community (PlusPlus-ua/ha_tuya_ble) and
confirmed against a Euhomy CFC-25 by lyntoo/ha-euhomy-ble.

Message layout (before fragmentation)::

    [security_flag: 1][iv: 16][AES-128-CBC(key, iv, inner)]
    inner = [seq: 4][response_to: 4][code: 2][length: 2][data][crc16: 2][zero padding to 16]

Fragmentation (20 byte GATT writes)::

    first fragment : varint(0) varint(total_length) [protocol_version << 4] chunk
    later fragments: varint(n) chunk

Keys (``local_key_6`` is the first six characters of the Tuya local key)::

    login_key   = MD5(local_key_6)                  # security flag 4, DEVICE_INFO only
    session_key = MD5(local_key_6 + srand)          # security flag 5, everything else

Connection sequence: DEVICE_INFO (device answers with ``srand``) -> PAIR
(``uuid + local_key_6 + device_id``, zero padded to 44 bytes) -> DEVICE_STATUS.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
import hashlib
import logging
import secrets
import struct
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

SERVICE_UUID = "00001910-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "00002b10-0000-1000-8000-00805f9b34fb"  # "Module->Phone"
WRITE_CHAR_UUID = "00002b11-0000-1000-8000-00805f9b34fb"  # "Phone->Module"

GATT_MTU = 20
PROTOCOL_VERSION = 2
PAIR_PAYLOAD_LENGTH = 44
MAX_UUID_LENGTH = 16
MAX_DEVICE_ID_LENGTH = 22

RESPONSE_TIMEOUT = 15.0

SECURITY_FLAG_LOGIN_KEY = 4
SECURITY_FLAG_SESSION_KEY = 5

PAIR_OK = 0
PAIR_ALREADY_PAIRED = 2


class Code(IntEnum):
    """Tuya BLE command codes."""

    DEVICE_INFO = 0x0000
    PAIR = 0x0001
    SEND_DPS = 0x0002
    DEVICE_STATUS = 0x0003
    RECEIVE_DP = 0x8001
    RECEIVE_TIME_DP = 0x8003
    RECEIVE_SIGN_DP = 0x8004
    RECEIVE_SIGN_TIME_DP = 0x8005
    TIME1_REQUEST = 0x8011
    TIME2_REQUEST = 0x8012


class DpType(IntEnum):
    """Tuya data point types."""

    RAW = 0
    BOOL = 1
    INT = 2
    STRING = 3
    ENUM = 4
    BITMAP = 5


class TuyaBLEError(Exception):
    """Base class for all client errors."""


class TuyaBLEConnectionError(TuyaBLEError):
    """The BLE link could not be established or was lost."""


class TuyaBLETimeoutError(TuyaBLEError):
    """The device did not answer in time."""


class TuyaBLEAuthError(TuyaBLEError):
    """The device rejected the pairing credentials."""


class TuyaBLEProtocolError(TuyaBLEError):
    """A frame could not be decoded."""


class TuyaBLEWriteError(TuyaBLEError):
    """The device refused a data point write."""


@dataclass(frozen=True, slots=True)
class DataPoint:
    """A single Tuya data point."""

    dp_id: int
    dp_type: DpType
    value: bool | int | str | bytes


@dataclass(frozen=True, slots=True)
class Message:
    """A decrypted Tuya BLE message."""

    seq: int
    response_to: int
    code: int
    data: bytes


# ── Primitives ────────────────────────────────────────────────────────────────


def crc16(data: bytes) -> int:
    """CRC-16/MODBUS."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def pack_varint(value: int) -> bytes:
    """Encode an unsigned integer as a 7-bit little-endian varint."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def unpack_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a varint at ``pos``; returns ``(value, next_pos)``."""
    result = 0
    for shift in range(0, 35, 7):
        if pos >= len(data):
            raise TuyaBLEProtocolError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
    raise TuyaBLEProtocolError("varint too long")


def derive_login_key(local_key: str) -> bytes:
    """Key used for the DEVICE_INFO request."""
    return hashlib.md5(local_key[:6].encode("ascii")).digest()


def derive_session_key(local_key: str, srand: bytes) -> bytes:
    """Key used once the device has handed out ``srand``."""
    return hashlib.md5(local_key[:6].encode("ascii") + srand).digest()


def _cipher(key: bytes, iv: bytes) -> Cipher:
    return Cipher(algorithms.AES(key), modes.CBC(iv))


def encrypt_message(
    *,
    key: bytes,
    security_flag: int,
    seq: int,
    code: int,
    data: bytes,
    response_to: int = 0,
    iv: bytes | None = None,
) -> bytes:
    """Build the encrypted payload for one message."""
    iv = iv if iv is not None else secrets.token_bytes(16)
    inner = struct.pack(">IIHH", seq, response_to, code, len(data)) + data
    inner += struct.pack(">H", crc16(inner))
    inner += b"\x00" * (-len(inner) % 16)
    encryptor = _cipher(key, iv).encryptor()
    return bytes([security_flag]) + iv + encryptor.update(inner) + encryptor.finalize()


def decrypt_message(payload: bytes, keys: dict[int, bytes | None]) -> Message:
    """Decrypt and validate a reassembled payload."""
    if len(payload) < 1 + 16 + 16:
        raise TuyaBLEProtocolError(f"payload too short ({len(payload)} bytes)")
    key = keys.get(payload[0])
    if key is None:
        raise TuyaBLEProtocolError(f"no key for security flag {payload[0]}")
    iv, ciphertext = payload[1:17], payload[17:]
    if len(ciphertext) % 16:
        raise TuyaBLEProtocolError("ciphertext is not block aligned")
    decryptor = _cipher(key, iv).decryptor()
    raw = decryptor.update(ciphertext) + decryptor.finalize()

    seq, response_to, code, length = struct.unpack(">IIHH", raw[:12])
    end = 12 + length
    if end + 2 > len(raw):
        raise TuyaBLEProtocolError("bad length field (wrong key?)")
    if struct.unpack(">H", raw[end : end + 2])[0] != crc16(raw[:end]):
        raise TuyaBLEProtocolError("CRC mismatch (wrong key?)")
    return Message(seq=seq, response_to=response_to, code=code, data=raw[12:end])


def split_into_packets(payload: bytes, mtu: int = GATT_MTU) -> list[bytes]:
    """Fragment a payload into GATT-sized packets."""
    packets: list[bytes] = []
    pos = 0
    number = 0
    while pos < len(payload):
        header = pack_varint(number)
        if number == 0:
            header += pack_varint(len(payload)) + bytes([PROTOCOL_VERSION << 4])
        chunk = payload[pos : pos + mtu - len(header)]
        packets.append(header + chunk)
        pos += len(chunk)
        number += 1
    return packets


class Reassembler:
    """Reassemble fragmented notifications into payloads."""

    def __init__(self) -> None:
        self._buffer: bytearray | None = None
        self._next = 0
        self._length = 0

    def reset(self) -> None:
        """Drop any partially received payload."""
        self._buffer = None
        self._next = 0
        self._length = 0

    def feed(self, fragment: bytes) -> bytes | None:
        """Add a fragment; returns the payload once it is complete."""
        number, pos = unpack_varint(fragment, 0)
        if number == 0:
            self._length, pos = unpack_varint(fragment, pos)
            pos += 1  # protocol version byte
            self._buffer = bytearray()
            self._next = 1
        elif self._buffer is not None and number == self._next:
            self._next += 1
        else:
            self.reset()
            raise TuyaBLEProtocolError(f"unexpected fragment {number}")

        assert self._buffer is not None
        self._buffer += fragment[pos:]
        if len(self._buffer) < self._length:
            return None
        payload = bytes(self._buffer[: self._length])
        self.reset()
        return payload


# ── Data points ───────────────────────────────────────────────────────────────


def encode_dp(dp: DataPoint) -> bytes:
    """Serialise one data point: ``[id][type][length][value]``."""
    if dp.dp_type is DpType.BOOL:
        raw = bytes([1 if dp.value else 0])
    elif dp.dp_type is DpType.INT:
        raw = struct.pack(">i", int(dp.value))
    elif dp.dp_type is DpType.ENUM:
        raw = bytes([int(dp.value)])
    elif dp.dp_type is DpType.BITMAP:
        raw = struct.pack(">I", int(dp.value))
    elif dp.dp_type is DpType.STRING:
        raw = str(dp.value).encode()
    else:
        raw = bytes(dp.value)  # type: ignore[arg-type]
    return struct.pack(">BBB", dp.dp_id, dp.dp_type, len(raw)) + raw


def decode_dps(data: bytes) -> list[DataPoint]:
    """Parse a data point stream; stops at the first malformed entry."""
    dps: list[DataPoint] = []
    pos = 0
    while pos + 3 <= len(data):
        dp_id, raw_type, length = data[pos], data[pos + 1], data[pos + 2]
        raw = data[pos + 3 : pos + 3 + length]
        if len(raw) != length:
            _LOGGER.debug("Truncated data point %d in %s", dp_id, data.hex())
            break
        pos += 3 + length
        try:
            dp_type = DpType(raw_type)
        except ValueError:
            _LOGGER.debug("Unknown data point type %d for dp %d", raw_type, dp_id)
            continue

        value: bool | int | str | bytes
        if dp_type is DpType.BOOL:
            value = bool(raw[0]) if raw else False
        elif dp_type is DpType.INT:
            value = int.from_bytes(raw, "big", signed=True)
        elif dp_type is DpType.ENUM:
            value = raw[0] if raw else 0
        elif dp_type is DpType.BITMAP:
            value = int.from_bytes(raw, "big")
        elif dp_type is DpType.STRING:
            value = raw.decode(errors="replace")
        else:
            value = raw
        dps.append(DataPoint(dp_id, dp_type, value))
    return dps


# ── Client ────────────────────────────────────────────────────────────────────


def resolve_characteristics(client: BleakClient) -> tuple[str, str]:
    """Return the ``(notify, write)`` characteristic UUIDs of the Tuya service.

    The usual 0x2b10/0x2b11 pair is used when present. Otherwise the
    characteristics of service 0x1910 are picked by their properties, which is
    how the "Module->Phone" (notify) and "Phone->Module" (write) pair shows up
    in a GATT browser.
    """
    services = client.services
    if (
        services.get_characteristic(NOTIFY_CHAR_UUID) is not None
        and services.get_characteristic(WRITE_CHAR_UUID) is not None
    ):
        return NOTIFY_CHAR_UUID, WRITE_CHAR_UUID

    service = services.get_service(SERVICE_UUID)
    if service is None:
        found = sorted(str(s.uuid) for s in services)
        raise TuyaBLEConnectionError(f"Tuya BLE service 0x1910 not found (services: {found})")
    notify = next((c for c in service.characteristics if "notify" in c.properties), None)
    write = next(
        (c for c in service.characteristics if "write-without-response" in c.properties),
        None,
    ) or next((c for c in service.characteristics if "write" in c.properties), None)
    if notify is None or write is None:
        found = [(str(c.uuid), sorted(c.properties)) for c in service.characteristics]
        raise TuyaBLEConnectionError(
            f"Tuya BLE notify/write characteristics not found (characteristics: {found})"
        )
    _LOGGER.debug("Using characteristics notify=%s write=%s", notify.uuid, write.uuid)
    return str(notify.uuid), str(write.uuid)


def _timezone_hundredths(now: datetime) -> int:
    """UTC offset in hundredths of an hour, as the Tuya time replies expect."""
    offset = now.utcoffset()
    return int(offset.total_seconds() / 36) if offset else 0


class TuyaBLEClient:
    """Connection to one Tuya BLE device."""

    def __init__(
        self,
        *,
        local_key: str,
        device_id: str,
        uuid: str,
        on_dps: Callable[[list[DataPoint]], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._local_key = local_key
        self._device_id = device_id
        self._uuid = uuid
        self._on_dps = on_dps
        self._on_disconnect = on_disconnect
        self._now = now or (lambda: datetime.now().astimezone())

        self._login_key = derive_login_key(local_key)
        self._session_key: bytes | None = None

        self._client: BleakClient | None = None
        self._connected = False
        self._closing = False
        self._seq = 0
        self._reassembler = Reassembler()
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._write_uuid = WRITE_CHAR_UUID
        self._pending: dict[int, tuple[int, asyncio.Future[bytes]]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

        self.device_info: bytes = b""

    @property
    def connected(self) -> bool:
        """True once the BLE link is up and the pairing handshake succeeded."""
        return self._connected

    # ── Public API ────────────────────────────────────────────────────────────

    async def connect(
        self,
        ble_device: BLEDevice,
        ble_device_callback: Callable[[], BLEDevice] | None = None,
    ) -> None:
        """Connect and run the DEVICE_INFO -> PAIR -> DEVICE_STATUS sequence."""
        async with self._connect_lock:
            if self._connected:
                return
            await self._teardown()
            self._closing = False
            self._session_key = None
            self._seq = 0
            self._reassembler.reset()

            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    ble_device.name or ble_device.address,
                    self._handle_disconnect,
                    use_services_cache=True,
                    ble_device_callback=ble_device_callback,
                )
            except (BleakError, TimeoutError, OSError) as err:
                raise TuyaBLEConnectionError(f"cannot connect: {err}") from err
            self._client = client

            try:
                notify_uuid, self._write_uuid = resolve_characteristics(client)
                try:
                    await client.start_notify(notify_uuid, self._on_notify)
                except BleakError as err:
                    raise TuyaBLEConnectionError(
                        f"Tuya BLE notify characteristic unavailable: {err}"
                    ) from err

                self.device_info = await self._request(Code.DEVICE_INFO, b"")
                if len(self.device_info) < 12:
                    raise TuyaBLEProtocolError("short DEVICE_INFO response")
                self._session_key = derive_session_key(self._local_key, self.device_info[6:12])

                result = await self._request(Code.PAIR, self._pair_payload())
                if not result or result[0] not in (PAIR_OK, PAIR_ALREADY_PAIRED):
                    raise TuyaBLEAuthError(
                        f"pairing rejected (result={result[0] if result else None})"
                    )
                self._connected = True
                await self.request_status()
            except BaseException:
                await self._teardown()
                raise

    async def disconnect(self) -> None:
        """Close the link without notifying ``on_disconnect``."""
        async with self._connect_lock:
            await self._teardown()

    async def request_status(self) -> None:
        """Ask the device to report all data points (also serves as keep-alive).

        The values arrive as separate data point reports, so a missing
        acknowledgement is not treated as an error; a dead link is reported by
        the Bluetooth stack instead.
        """
        try:
            await self._request(Code.DEVICE_STATUS, b"")
        except TuyaBLETimeoutError:
            _LOGGER.debug("No acknowledgement for the status request")

    async def send_dps(self, dps: Sequence[DataPoint]) -> None:
        """Write data points; raises if the device does not accept them."""
        payload = b"".join(encode_dp(dp) for dp in dps)
        _LOGGER.debug("Writing data points: %s", payload.hex())
        result = await self._request(Code.SEND_DPS, payload)
        if result and result[0] != 0:
            raise TuyaBLEWriteError(f"device rejected write (result={result[0]})")

    # ── Handshake helpers ─────────────────────────────────────────────────────

    def _pair_payload(self) -> bytes:
        payload = (
            self._uuid.encode("ascii")
            + self._local_key[:6].encode("ascii")
            + self._device_id.encode("ascii")
        )
        return payload.ljust(PAIR_PAYLOAD_LENGTH, b"\x00")[:PAIR_PAYLOAD_LENGTH]

    # ── Sending ───────────────────────────────────────────────────────────────

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return self._seq

    async def _request(self, code: Code, data: bytes) -> bytes:
        """Send a message and wait for the device's response to it."""
        seq = self._next_seq()
        future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._pending[seq] = (code, future)
        try:
            await self._write_message(code, data, seq)
            async with asyncio.timeout(RESPONSE_TIMEOUT):
                return await future
        except TimeoutError as err:
            raise TuyaBLETimeoutError(f"no response to {code.name}") from err
        finally:
            self._pending.pop(seq, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # mark as retrieved

    async def _write_message(self, code: int, data: bytes, seq: int, response_to: int = 0) -> None:
        client = self._client
        if client is None:
            raise TuyaBLEConnectionError("not connected")
        if code == Code.DEVICE_INFO:
            key, flag = self._login_key, SECURITY_FLAG_LOGIN_KEY
        else:
            key, flag = self._session_key or self._login_key, SECURITY_FLAG_SESSION_KEY
        payload = encrypt_message(
            key=key,
            security_flag=flag,
            seq=seq,
            code=code,
            data=data,
            response_to=response_to,
        )
        try:
            async with self._write_lock:
                for packet in split_into_packets(payload):
                    await client.write_gatt_char(self._write_uuid, packet, response=False)
        except (BleakError, OSError) as err:
            raise TuyaBLEConnectionError(f"write failed: {err}") from err

    def _respond(self, code: int, data: bytes, response_to: int) -> None:
        """Send a reply to a device-initiated message without blocking the notify callback."""

        async def _send() -> None:
            try:
                await self._write_message(code, data, self._next_seq(), response_to)
            except TuyaBLEError as err:
                _LOGGER.debug("Could not answer 0x%04x: %s", code, err)

        task = asyncio.get_running_loop().create_task(_send())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── Receiving ─────────────────────────────────────────────────────────────

    def _on_notify(self, _char: Any, data: bytearray) -> None:
        _LOGGER.debug("Notification (%d B): %s", len(data), bytes(data).hex())
        try:
            payload = self._reassembler.feed(bytes(data))
            if payload is None:
                return
            message = decrypt_message(
                payload,
                {
                    SECURITY_FLAG_LOGIN_KEY: self._login_key,
                    SECURITY_FLAG_SESSION_KEY: self._session_key,
                },
            )
        except (TuyaBLEProtocolError, ValueError, struct.error) as err:
            _LOGGER.warning("Dropping undecodable notification: %s", err)
            self._reassembler.reset()
            return
        _LOGGER.debug(
            "Message seq=%d response_to=%d code=0x%04x data=%s",
            message.seq,
            message.response_to,
            message.code,
            message.data.hex(),
        )
        self._dispatch(message)

    def _dispatch(self, message: Message) -> None:
        pending = self._pending.get(message.response_to)
        if pending is not None and pending[0] == message.code:
            if not pending[1].done():
                pending[1].set_result(message.data)
            return

        code = message.code
        if code == Code.RECEIVE_DP:
            self._handle_dp_push(message, offset=0, signed=False)
        elif code == Code.RECEIVE_TIME_DP:
            self._handle_dp_push(message, offset=15, signed=False)
        elif code == Code.RECEIVE_SIGN_DP:
            self._handle_dp_push(message, offset=3, signed=True)
        elif code == Code.RECEIVE_SIGN_TIME_DP:
            self._handle_dp_push(message, offset=18, signed=True)
        elif code == Code.TIME1_REQUEST:
            now = self._now()
            reply = str(int(now.timestamp() * 1000)).encode() + struct.pack(
                ">h", _timezone_hundredths(now)
            )
            self._respond(code, reply, message.seq)
        elif code == Code.TIME2_REQUEST:
            now = self._now()
            reply = struct.pack(
                ">BBBBBBBh",
                now.year % 100,
                now.month,
                now.day,
                now.hour,
                now.minute,
                now.second,
                now.weekday(),
                _timezone_hundredths(now),
            )
            self._respond(code, reply, message.seq)
        else:
            _LOGGER.debug("Unhandled message 0x%04x: %s", code, message.data.hex())

    def _handle_dp_push(self, message: Message, *, offset: int, signed: bool) -> None:
        """Parse and acknowledge a data point report.

        Only RECEIVE_DP has been observed on a Euhomy fridge. The offsets of the
        timestamped variants follow PlusPlus-ua/ha_tuya_ble.
        """
        dps = decode_dps(message.data[offset:])
        if dps and self._on_dps:
            self._on_dps(dps)
        elif not dps and message.data[offset:]:
            _LOGGER.warning(
                "Could not parse data points of 0x%04x: %s",
                message.code,
                message.data.hex(),
            )

        if signed and len(message.data) >= 3:
            dp_seq = int.from_bytes(message.data[:2], "big")
            ack = struct.pack(">HBB", dp_seq, message.data[2], 0)
        else:
            ack = b""
        self._respond(message.code, ack, message.seq)

    # ── Teardown ──────────────────────────────────────────────────────────────

    def _handle_disconnect(self, client: BleakClient) -> None:
        """Bleak disconnect callback."""
        if self._client is not None and client is not self._client:
            return  # a previous connection reporting late
        was_connected = self._connected
        self._connected = False
        self._fail_pending()
        if was_connected and not self._closing and self._on_disconnect:
            self._on_disconnect()

    def _fail_pending(self) -> None:
        for _code, future in self._pending.values():
            if not future.done():
                future.set_exception(TuyaBLEConnectionError("disconnected"))

    async def _teardown(self) -> None:
        self._closing = True
        self._connected = False
        self._fail_pending()
        for task in list(self._tasks):
            task.cancel()
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except (BleakError, TimeoutError, OSError):
                _LOGGER.debug("Error while disconnecting", exc_info=True)
