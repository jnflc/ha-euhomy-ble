"""A simulated Tuya BLE peripheral that speaks the device side of the protocol."""

from __future__ import annotations

from collections.abc import Callable

from custom_components.euhomy_ble.tuya_ble import (
    SECURITY_FLAG_LOGIN_KEY,
    SECURITY_FLAG_SESSION_KEY,
    Code,
    DataPoint,
    Message,
    Reassembler,
    decode_dps,
    decrypt_message,
    derive_login_key,
    derive_session_key,
    encode_dp,
    encrypt_message,
    split_into_packets,
)

LOCAL_KEY = "abcdef0123456789"
DEVICE_ID = "d4b9e7fgm2nkw3xp"
UUID = "3c7a2f5d8b4e9106"
SRAND = bytes.fromhex("a1b2c3d4e5f6")


class FakeDevice:
    """Decrypts what the client writes and answers like a Euhomy fridge."""

    def __init__(
        self,
        *,
        local_key: str = LOCAL_KEY,
        device_id: str = DEVICE_ID,
        uuid: str = UUID,
        pair_result: int = 0,
        write_result: int = 0,
        initial_dps: list[DataPoint] | None = None,
        answer_info: bool = True,
        answer_status: bool = True,
    ) -> None:
        self.local_key = local_key
        self.device_id = device_id
        self.uuid = uuid
        self.pair_result = pair_result
        self.write_result = write_result
        self.initial_dps = initial_dps or []
        self.answer_info = answer_info
        self.answer_status = answer_status

        self.notify: Callable[[object, bytearray], None] | None = None
        self.received: list[Message] = []
        self.written_dps: list[DataPoint] = []
        self.replies: dict[int, Message] = {}
        self._reassembler = Reassembler()
        self._seq = 0
        self._session_key: bytes | None = None

    # ── device -> phone ───────────────────────────────────────────────────────

    def _send(self, code: int, data: bytes, response_to: int = 0, *, login: bool = False) -> None:
        self._seq += 1
        payload = encrypt_message(
            key=derive_login_key(self.local_key) if login else self._session_key,
            security_flag=SECURITY_FLAG_LOGIN_KEY if login else SECURITY_FLAG_SESSION_KEY,
            seq=self._seq,
            response_to=response_to,
            code=code,
            data=data,
        )
        assert self.notify is not None
        for packet in split_into_packets(payload):
            self.notify(None, bytearray(packet))

    def push_dps(self, dps: list[DataPoint], code: int = Code.RECEIVE_DP) -> None:
        """Report data points on the device's own initiative."""
        self._send(code, b"".join(encode_dp(dp) for dp in dps))

    def request(self, code: int, data: bytes = b"") -> None:
        """Send a device-initiated request such as a time query."""
        self._send(code, data)

    # ── phone -> device ───────────────────────────────────────────────────────

    def on_write(self, packet: bytes) -> None:
        payload = self._reassembler.feed(packet)
        if payload is None:
            return
        keys = {
            SECURITY_FLAG_LOGIN_KEY: derive_login_key(self.local_key),
            SECURITY_FLAG_SESSION_KEY: self._session_key,
        }
        message = decrypt_message(payload, keys)
        self.received.append(message)

        if message.code == Code.DEVICE_INFO:
            if not self.answer_info:
                return
            self._session_key = derive_session_key(self.local_key, SRAND)
            info = bytes([1, 2, 3, 4, 0, 0]) + SRAND + bytes([0, 1]) + bytes(32)
            self._send(Code.DEVICE_INFO, info, message.seq, login=True)
        elif message.code == Code.PAIR:
            expected = (
                self.uuid.encode() + self.local_key[:6].encode() + self.device_id.encode()
            ).ljust(44, b"\x00")
            result = self.pair_result if message.data == expected else 1
            self._send(Code.PAIR, bytes([result]), message.seq)
        elif message.code == Code.DEVICE_STATUS:
            if not self.answer_status:
                return
            self._send(Code.DEVICE_STATUS, b"\x00", message.seq)
            if self.initial_dps:
                self.push_dps(self.initial_dps)
        elif message.code == Code.SEND_DPS:
            if self.write_result == 0:
                self.written_dps.extend(decode_dps(message.data))
            self._send(Code.SEND_DPS, bytes([self.write_result]), message.seq)
        else:
            self.replies[message.code] = message
