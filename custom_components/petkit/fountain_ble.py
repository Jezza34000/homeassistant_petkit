"""Native BLE coordinator for PetKit water fountains.

Provides direct Bluetooth communication with PetKit water fountains
(W4/W5/CTW2/CTW3) using Home Assistant's bluetooth component, which
transparently supports both local Bluetooth adapters and ESPHome
Bluetooth proxies.

This module is only used when the user enables 'Local BLE' mode in the
integration options. It runs independently of the cloud coordinator and
updates the WaterFountain entities directly from BLE data.

Debug log file
--------------
When the *local_ble_debug_log* option is enabled, BLE messages are
written to ``<config_dir>/petkit.log`` (one rotating 1 MB backup).
No file is created when debug logging is disabled.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import logging
import logging.handlers
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from bleak import BleakClient

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File-based debug logger
# ---------------------------------------------------------------------------

_FILE_HANDLER_ATTACHED: bool = False


def _setup_ble_log_file(config_dir: str, *, debug_enabled: bool = False) -> None:
    """Attach a rotating file handler to the BLE logger when debug is enabled.

    When *debug_enabled* is True, a RotatingFileHandler writing DEBUG-level
    messages to <config_dir>/petkit.log is attached (once per HA run).
    When *debug_enabled* is False, no file is created or written to and the
    logger level is left at INFO.
    """
    global _FILE_HANDLER_ATTACHED  # noqa: PLW0603

    if not debug_enabled:
        _LOGGER.setLevel(logging.INFO)
        return

    log_path = Path(config_dir) / "petkit.log"

    if not _FILE_HANDLER_ATTACHED:
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=1_048_576,  # 1 MB
            backupCount=1,
            encoding="utf-8",
        )
        fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        handler.setFormatter(fmt)
        handler.setLevel(logging.DEBUG)
        _LOGGER.addHandler(handler)
        _FILE_HANDLER_ATTACHED = True
        _LOGGER.info("PetKit BLE log file: %s (fountain_ble v10-cmd210)", log_path)

    _LOGGER.setLevel(logging.DEBUG)


# BLE GATT characteristic UUIDs for PetKit fountains
BLE_NOTIFY_UUID = "0000aaa1-0000-1000-8000-00805f9b34fb"
BLE_WRITE_UUID = "0000aaa2-0000-1000-8000-00805f9b34fb"

# BLE advertisement name prefixes for all supported fountain models
BLE_FOUNTAIN_NAME_PREFIXES = (
    "Petkit_CTW3",
    "Petkit_CTW2",
    "Petkit_W5C",
    "Petkit_W5N",
    "Petkit_W5",
    "Petkit_W4XUVC",
    "Petkit_W4X",
)


class LocalFountainBleProtocol:
    """BLE protocol implementation for PetKit water fountains.

    Handles the low-level byte framing and parsing for local direct BLE
    communication. The protocol is derived from the open-source
    PetkitW5BLEMQTT project (MIT licence, slespersen/PetkitW5BLEMQTT).

    Frame format (little-endian):
        [0xFA, 0xFC, 0xFD, cmd, type, seq, data_len, 0x00, ...data, 0xFB]

    Relevant command codes:
        213  Request device ID (response carries device_id_bytes)
         73  Authenticate with device_id + secret
         86  Sync using secret
         84  Set device clock
        210  Request device state (CTW3 primary poll)
        211  Request device configuration
        230  Request full status (state + config in one payload)
        220  Set power state / operating mode
        221  Set configuration block
        222  Reset filter life counter
    """

    _FRAME_START = [0xFA, 0xFC, 0xFD]
    _FRAME_END = [0xFB]

    _CMD_DEVICE_ID: int = 213
    _CMD_AUTH: int = 73
    _CMD_SYNC: int = 86
    _CMD_SET_TIME: int = 84
    _CMD_STATE: int = 210  # device state  (CTW3 26-byte payload)
    _CMD_CONFIG_READ: int = 211  # device config (CTW3 10-byte payload)
    _CMD_STATUS: int = 230  # combined state+config (type=2 request)
    _CMD_MODE: int = 220
    _CMD_CONFIG: int = 221
    _CMD_RESET_FILTER: int = 222

    def __init__(self, alias: str, mac_bytes: list[int] | None = None) -> None:
        """Initialise for the given device alias (e.g. 'CTW3', 'W5').

        The CTW3 / Eversweet Max 2 uses an all-zero device ID with the magic
        trailing bytes [13, 37] — the device validates this in CMD 86.  Using
        the MAC address as device ID causes CMD 86 to fail (response 00) and
        the device disconnects.  The ``mac_bytes`` parameter is kept for
        signature compatibility but is intentionally not used.
        """
        self._alias = alias
        self._seq: int = 0
        self._device_id_bytes: list[int] = []
        self._secret: list[int] = []
        self._device_id_received: bool = False
        self._recv_buffer: bytearray = bytearray()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def device_id_received(self) -> bool:
        """True after a CMD 213 response has been successfully parsed."""
        return self._device_id_received

    @property
    def current_secret(self) -> list[int]:
        """The 8-byte secret used for the most recent CMD 73/86 sequence.

        Captured for the per-device BLE secret cache (``CONF_BLE_SECRETS``)
        so subsequent local-BLE handshakes can reuse the value that the
        device accepted, instead of re-deriving from CMD 213.
        """
        return list(self._secret)

    # ------------------------------------------------------------------
    # Command builders
    # ------------------------------------------------------------------

    def get_init_commands(self) -> list[bytearray]:
        """Return the first command in the handshake (CMD 213 – device ID)."""
        return [self._build_frame(self._CMD_DEVICE_ID, 1, [0, 0])]

    def complete_init_commands(
        self, secret_override: list[int] | None = None
    ) -> list[bytearray]:
        """Return auth (CMD 73), sync (CMD 86) and set-time (CMD 84) frames.

        If *secret_override* is provided (8 bytes), it bypasses the per-model
        derivation entirely. This lets a previously-captured per-device secret
        (CMD 86 success → ``CONF_BLE_SECRETS``) be reused so the cloud-relay
        session survives the local-BLE handshake (Jezza34000, PR #203 design
        comment 2026-05-02).

        The CTW3 / Eversweet Max 2 auth always succeeds when the device_id used
        for the secret derivation is all-zeros, even if CMD 213 returned non-zero
        bytes.  Using the actual CMD 213 bytes (partial MAC) causes CMD 86 to fail
        with response ``00`` and the device disconnects.  So for CTW3 we always
        derive the secret from zeros.

        CMD 73 and CMD 86 must always be sent — the device validates CMD 86
        and disconnects if auth is skipped.
        """
        cmds: list[bytearray] = []

        # For CTW3: always use zeros as device_id for auth computation.
        # For other models: use what CMD 213 returned (or zeros if not received).
        if self._alias == "CTW3":
            device_id = [0] * 6
        else:
            device_id = self._device_id_bytes or [0] * 6

        device_id_padded = [0] * (8 - len(device_id)) + device_id

        if secret_override is not None and len(secret_override) == 8:
            self._secret = list(secret_override)
            _LOGGER.debug(
                "Auth: device_id=%s secret=%s (cached)",
                bytes(device_id_padded).hex(),
                bytes(self._secret).hex(),
            )
        else:
            secret = list(reversed(device_id))
            # Replace the last two bytes of the (unpadded) reversed ID with
            # the magic constant [13, 37] when they are both zero.
            if len(secret) >= 2 and secret[-1] == 0 and secret[-2] == 0:
                secret[-2] = 13
                secret[-1] = 37
            self._secret = [0] * (8 - len(secret)) + secret
            _LOGGER.debug(
                "Auth: device_id=%s secret=%s",
                bytes(device_id_padded).hex(),
                bytes(self._secret).hex(),
            )
        cmds.append(
            self._build_frame(
                self._CMD_AUTH, 1, [0, 0, *device_id_padded, *self._secret]
            )
        )
        cmds.append(self._build_frame(self._CMD_SYNC, 1, [0, 0, *self._secret]))

        cmds.append(self._build_frame(self._CMD_SET_TIME, 1, self._time_bytes()))
        return cmds

    def get_status_command(self, data: list[int] | None = None) -> bytearray:
        """Return CMD 230 – request full status (state + config combined).

        IMPORTANT: The CTW3 / Eversweet Max 2 requires type=2 for CMD 230
        requests (confirmed from the petkit_ble_mqtt reference implementation).
        type=1 is used only for unsolicited proactive pushes FROM the device.
        *data* defaults to ``[0x01]``.
        """
        return self._build_frame(self._CMD_STATUS, 2, data if data is not None else [1])

    def get_device_state_command(self) -> bytearray:
        """Return CMD 210 – request device state (CTW3 26-byte payload).

        An alternative to CMD 230 when the combined-status command is
        unavailable or unresponsive.  Uses type=1 with data=[0, 0].
        """
        return self._build_frame(self._CMD_STATE, 1, [0, 0])

    def get_device_config_command(self) -> bytearray:
        """Return CMD 211 – request device configuration (CTW3 10-byte payload)."""
        return self._build_frame(self._CMD_CONFIG_READ, 1, [0, 0])

    def build_set_mode_command(self, power_state: int, mode: int) -> bytearray:
        """Return CMD 220 – set power state and operating mode."""
        return self._build_frame(self._CMD_MODE, 1, [power_state, mode])

    def build_set_config_command(self, config_data: list[int]) -> bytearray:
        """Return CMD 221 – write a configuration block."""
        return self._build_frame(self._CMD_CONFIG, 1, config_data)

    def get_reset_filter_command(self) -> bytearray:
        """Return CMD 222 – reset the filter life counter."""
        return self._build_frame(self._CMD_RESET_FILTER, 1, [0])

    # ------------------------------------------------------------------
    # Notification handler
    # ------------------------------------------------------------------

    def handle_notification(self, data: bytearray) -> dict[str, Any] | None:
        """Parse an incoming BLE notification frame, handling multi-packet responses.

        BLE notifications may arrive in chunks if the payload exceeds the negotiated
        MTU. This method accumulates bytes in ``_recv_buffer`` until a complete frame
        (ending with 0xFB) is detected, then dispatches the full frame.

        Returns a status dict when a complete CMD 230 frame is received, None otherwise.
        CMD 213 responses are consumed internally to set device_id_received.
        """
        _LOGGER.debug(
            "BLE notification chunk raw=%s",
            data.hex(),
        )

        # A new frame always starts with the 3-byte preamble FA FC FD.
        # If this chunk begins a new frame, flush any leftover partial data first.
        if len(data) >= 3 and data[0] == 0xFA and data[1] == 0xFC and data[2] == 0xFD:
            self._recv_buffer = bytearray()

        self._recv_buffer.extend(data)

        # Check if the buffer ends with the frame terminator 0xFB.
        if not self._recv_buffer.endswith(b"\xfb"):
            _LOGGER.debug(
                "BLE partial frame, buffering (%d bytes so far)", len(self._recv_buffer)
            )
            return None

        frame = bytes(self._recv_buffer)
        self._recv_buffer = bytearray()

        if len(frame) < 9:
            _LOGGER.warning(
                "BLE frame too short (%d bytes): %s", len(frame), frame.hex()
            )
            return None

        # Frame layout: [FA, FC, FD, cmd, type, seq, data_len, 0x00, ...payload, FB]
        cmd = frame[3]
        payload = bytes(frame[8:-1])
        _LOGGER.debug(
            "BLE complete frame cmd=%d payload_len=%d payload=%s",
            cmd,
            len(payload),
            payload.hex(),
        )

        if cmd == self._CMD_DEVICE_ID:
            self._parse_device_id(payload)
            return None

        if cmd == self._CMD_STATE:
            # CMD 210: device state — same CTW3 layout as CMD 230 for the first 26+ bytes
            return self._parse_status(payload)

        if cmd == self._CMD_CONFIG_READ:
            # CMD 211: device configuration — return with a special marker so the
            # client can merge it into the existing status rather than overwriting.
            config = self._parse_config_ctw3(payload)
            if config:
                config["_config_only"] = True
                return config
            return None

        if cmd == self._CMD_STATUS:
            return self._parse_status(payload)

        return None

    # ------------------------------------------------------------------
    # Static helper – update HA entity from status dict
    # ------------------------------------------------------------------

    @staticmethod
    def _update_status_fields(s: Any, status: dict[str, Any]) -> None:
        """Apply BLE status keys to the Status sub-model."""
        if "power_status" in status:
            s.power_status = status["power_status"]
        if "suspend_status" in status:
            s.suspend_status = status.get("suspend_status")
        if "detect_status" in status:
            s.detect_status = status.get("detect_status")
        if "electric_status" in status:
            s.electric_status = status.get("electric_status")
        if "running_status" in status:
            s.run_status = status["running_status"]

    @staticmethod
    def _update_electricity_fields(e: Any, status: dict[str, Any]) -> None:
        """Apply BLE status keys to the Electricity sub-model."""
        if "battery_percentage" in status:
            e.battery_percent = status["battery_percentage"]
        if "battery_voltage" in status:
            e.battery_voltage = status["battery_voltage"]
        if "supply_voltage" in status:
            e.supply_voltage = status["supply_voltage"]

    @staticmethod
    def update_water_fountain(device: Any, status: dict[str, Any]) -> None:
        """Apply a BLE status dict to a WaterFountain Pydantic model instance."""
        from pypetkitapi.water_fountain_container import Electricity, Status

        if device.status is None:
            device.status = Status()
        if device.electricity is None:
            device.electricity = Electricity()

        LocalFountainBleProtocol._update_status_fields(device.status, status)
        LocalFountainBleProtocol._update_electricity_fields(device.electricity, status)

        if "mode" in status:
            device.mode = status["mode"]
        if "warning_water_missing" in status:
            device.lack_warning = status["warning_water_missing"]
        if "low_battery" in status:
            device.low_battery = status["low_battery"]
        if "warning_filter" in status:
            device.filter_warning = status["warning_filter"]
        if "filter_percentage" in status:
            device.filter_percent = int(status["filter_percentage"])
        if "pump_runtime_today" in status:
            device.today_pump_run_time = status["pump_runtime_today"]
        if "pump_runtime" in status:
            device.water_pump_run_time = status["pump_runtime"]
        if "module_status" in status:
            device.module_status = status["module_status"]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_frame(self, cmd: int, type_: int, data: list[int]) -> bytearray:
        """Build a BLE command frame and advance the sequence counter."""
        frame = [
            *self._FRAME_START,
            cmd,
            type_,
            self._seq,
            len(data),
            0,
            *data,
            *self._FRAME_END,
        ]
        self._seq = (self._seq + 1) & 0xFF
        return bytearray(frame)

    def _parse_device_id(self, payload: bytes) -> None:
        """Extract device ID bytes from a CMD 213 response payload.

        The CTW3 / Eversweet Max 2 deliberately returns all-zero bytes at
        payload[2:8].  This is intentional: CMD 86 (sync) is validated against
        a secret derived from those zeros, and the device disconnects if any
        other value (e.g. the MAC address) is used.
        """
        if len(payload) >= 8:
            self._device_id_bytes = list(payload[2:8])
            _LOGGER.debug(
                "CMD 213 device ID bytes: %s%s",
                bytes(self._device_id_bytes).hex(),
                (
                    " (all zeros — CTW3 uses zero-based auth)"
                    if not any(self._device_id_bytes)
                    else ""
                ),
            )
            self._device_id_received = True

    def _parse_status(self, payload: bytes) -> dict[str, Any] | None:
        """Parse a CMD 230 full-status payload into a normalised dict."""
        if self._alias == "CTW3":
            return self._parse_status_ctw3(payload)
        return self._parse_status_generic(payload)

    def _parse_status_ctw3(self, payload: bytes) -> dict[str, Any] | None:
        """Parse CMD 230 / CMD 210 status for CTW3/Eversweet Max 2.

        Byte layout confirmed against the petkit_ble_mqtt reference implementation
        (Delido/hassio-addons) and cross-checked with live device data:

          [0]     power_status          (1=on, 0=off)
          [1]     suspend_status
          [2]     mode                  (1=normal, 2=smart)
          [3]     electric_status       (2=AC power)
          [4]     dnd_state
          [5]     warning_breakdown
          [6]     warning_water_missing
          [7]     low_battery
          [8]     warning_filter
          [9:13]  pump_runtime          big-endian uint32, seconds
          [13]    filter_percentage     0-100 raw integer
          [14]    running_status
          [15:19] pump_runtime_today    big-endian uint32, seconds
          [19]    detect_status / pet_drinking
          [20:22] supply_voltage        big-endian int16, millivolts
          [22:24] battery_voltage       big-endian int16, millivolts
          [24]    battery_percentage    0-100
          [25]    module_status
          [26]    smart_time_on         minutes (optional)
          [27]    smart_time_off        minutes (optional)

        Verified against session-3 payload (42 bytes):
          data[9:13]  = 0x000038d0 = 14544 s  (4h pump runtime)
          data[13]    = 0x64 = 100            (100% filter)
          data[20:22] = 0x1232 = 4658 mV      (USB supply)
          data[22:24] = 0x1012 = 4114 mV      (Li-ion battery)
          data[24]    = 0x64 = 100            (100% battery)
        """
        _LOGGER.debug("CTW3 status payload (%d bytes): %s", len(payload), payload.hex())
        if len(payload) < 15:
            _LOGGER.warning("CTW3 status payload too short: %d bytes", len(payload))
            return None

        result: dict[str, Any] = {
            "power_status": payload[0],
            "suspend_status": payload[1],
            "mode_raw": payload[2],
            "electric_status": payload[3],
            "dnd_state": payload[4],
            "warning_breakdown": payload[5],
            "warning_water_missing": payload[6],
            "low_battery": payload[7],
            "warning_filter": payload[8],
            "pump_runtime": int.from_bytes(payload[9:13], "big"),
            "filter_percentage": payload[13],
            "running_status": payload[14],
        }
        # CTW3 firmware transiently reports mode=0 during the smart-mode sleep
        # phase (issue #57 in aavdberg/ha-petkit). Treating that as ground
        # truth would make the mode select show "Unknown" and any subsequent
        # power-switch toggle would send the wrong CMD 220 payload, kicking
        # the device out of smart mode. Only propagate ``mode`` when the
        # device reports a known value (1=normal, 2=smart); otherwise let the
        # consumer keep its previously latched mode.
        if payload[2] in (1, 2):
            result["mode"] = payload[2]

        if len(payload) >= 19:
            result["pump_runtime_today"] = int.from_bytes(payload[15:19], "big")
        if len(payload) >= 20:
            # Normalize to 0/1: CTW3 firmware 111 has been observed to use
            # value 2 (not 1) when the proximity sensor sees a pet. Treating
            # any non-zero value as "detected" makes the field future-proof
            # (issue #65 in aavdberg/ha-petkit) and keeps the drink-counter
            # increment logic working.
            result["detect_status"] = 1 if payload[19] else 0
        if len(payload) >= 22:
            result["supply_voltage"] = int.from_bytes(
                payload[20:22], "big", signed=True
            )
        if len(payload) >= 24:
            result["battery_voltage"] = int.from_bytes(
                payload[22:24], "big", signed=True
            )
        if len(payload) >= 25:
            result["battery_percentage"] = payload[24]
        if len(payload) >= 26:
            result["module_status"] = payload[25]

        return result

    def _parse_config_ctw3(self, payload: bytes) -> dict[str, Any] | None:
        """Parse CMD 211 (device configuration) for CTW3.

        Byte layout (matches build_settings_payload_ctw3 in aavdberg/ha-petkit
        v1.2.0 protocol.py — fixes off-by-one against earlier petkit_ble_mqtt
        documentation):
          [0]    smart_time_on        minutes (1-60)
          [1]    smart_time_off       minutes (1-60)
          [2:4]  battery_working_time big-endian uint16
          [4:6]  battery_sleep_time   big-endian uint16
          [6]    do_not_disturb_switch 0=off, 1=on
          [7]    led_switch           0=off, 1=on
          [8]    led_brightness       1=low, 2=medium, 3=high
          [9]    is_locked            (optional)

        Note: CTW3 firmware 111 has not been observed to reply to CMD 211 in
        practice, so this parser is currently unreached on real hardware but
        is kept symmetric with the CMD 221 builder layout.
        """
        _LOGGER.debug("CTW3 config payload (%d bytes): %s", len(payload), payload.hex())
        if len(payload) < 9:
            return None
        result: dict[str, Any] = {
            "smart_time_on": payload[0],
            "smart_time_off": payload[1],
            "do_not_disturb_switch": payload[6],
            "led_switch": payload[7],
            "led_brightness": payload[8],
        }
        if len(payload) >= 10:
            result["is_locked"] = payload[9]
        return result

    def _parse_status_generic(self, payload: bytes) -> dict[str, Any] | None:
        """Parse CMD 230 status for W4/W5/CTW2 family.

        Byte layout (confirmed from petkit_ble_mqtt parsers.py device_status()):
          [0]     power_status
          [1]     mode                  (1=normal, 2=smart)
          [2]     dnd_state
          [3]     warning_breakdown
          [4]     warning_water_missing
          [5]     warning_filter
          [6:10]  pump_runtime          big-endian uint32, seconds
          [10]    filter_percentage     0-100 raw integer
          [11]    running_status
          [12:16] pump_runtime_today    big-endian uint32, seconds
          [16]    smart_time_on         minutes (optional)
          [17]    smart_time_off        minutes (optional)
        """
        if len(payload) < 12:
            _LOGGER.debug("Generic status payload too short: %d bytes", len(payload))
            return None
        result: dict[str, Any] = {
            "power_status": payload[0],
            "mode": payload[1],
            "dnd_state": payload[2],
            "warning_breakdown": payload[3],
            "warning_water_missing": payload[4],
            "warning_filter": payload[5],
            "pump_runtime": int.from_bytes(payload[6:10], "big"),
            "filter_percentage": (payload[10] & 0xFF),
            "running_status": payload[11] & 0xFF,
        }
        if len(payload) >= 16:
            result["pump_runtime_today"] = int.from_bytes(payload[12:16], "big")
        if len(payload) >= 17:
            result["smart_time_on"] = payload[16]
        if len(payload) >= 18:
            result["smart_time_off"] = payload[17]
        return result

    @staticmethod
    def _time_bytes() -> list[int]:
        """Return 6 bytes encoding seconds since 2000-01-01 00:00:00 UTC."""
        ref = datetime(2000, 1, 1, tzinfo=timezone.utc)
        seconds = int((datetime.now(timezone.utc) - ref).total_seconds())
        return [
            0,
            (seconds >> 24) & 0xFF,
            (seconds >> 16) & 0xFF,
            (seconds >> 8) & 0xFF,
            seconds & 0xFF,
            13,  # protocol flag byte
        ]


BLE_NOTIFY_TIMEOUT = 10.0
# Delay (seconds) between commands in the init sequence (reference uses 0.75 s)
BLE_CMD_DELAY = 0.75


def is_petkit_fountain(service_info: BluetoothServiceInfoBleak) -> bool:
    """Return True if a BLE device is a supported PetKit fountain."""
    return service_info.name is not None and service_info.name.startswith(
        BLE_FOUNTAIN_NAME_PREFIXES
    )


def discovered_fountains(hass: HomeAssistant) -> list[BluetoothServiceInfoBleak]:
    """Return all currently discovered PetKit fountain BLE devices."""
    return [
        info
        for info in bluetooth.async_discovered_service_info(hass)
        if is_petkit_fountain(info)
    ]


def _alias_from_name(device_name: str) -> str:
    """Derive the device alias (e.g. 'CTW3', 'W5') from the BLE device name."""
    for prefix in (
        "Petkit_CTW3",
        "Petkit_CTW2",
        "Petkit_W5C",
        "Petkit_W5N",
        "Petkit_W5",
        "Petkit_W4XUVC",
        "Petkit_W4X",
    ):
        if device_name.startswith(prefix):
            return prefix.replace("Petkit_", "")
    return ""


class FountainBleClient:
    """Manages a direct BLE connection to a single PetKit fountain.

    Handles the full lifecycle:
    1. Obtain BLEDevice from HA bluetooth scanner
    2. Establish connection (with retry via bleak_retry_connector)
    3. Run initialization handshake
    4. Poll full status (CMD 230)
    5. Send control commands (mode, config)
    6. Disconnect cleanly
    """

    def __init__(
        self,
        hass: HomeAssistant,
        mac_address: str,
        device_name: str,
        *,
        debug_log: bool = False,
        cached_secret: list[int] | None = None,
        secret_persist_cb: Any | None = None,
        connect_grace_seconds: float = 15.0,
    ) -> None:
        """Initialize the BLE client for a specific fountain.

        ``cached_secret`` (8 bytes), if supplied, is reused by the protocol
        on the next handshake so a device that has already been paired by
        the cloud relay keeps its session intact.

        ``secret_persist_cb`` is an optional callable ``(mac, secret_hex)``
        invoked after a successful CMD 86 so the caller can write the value
        to ``entry.data[CONF_BLE_SECRETS]``. (Jezza34000 PR #203 design
        comment 2026-05-02.)

        ``connect_grace_seconds`` is the window during which we keep retrying
        ``bluetooth.async_ble_device_from_address`` when it returns ``None``
        (BLE adapter / proxy not yet ready). Defaults to 15 s.
        """
        self.hass = hass
        self.mac_address = mac_address
        self.device_name = device_name
        self._alias = _alias_from_name(device_name)
        self._protocol = LocalFountainBleProtocol(self._alias)
        self._client: BleakClient | None = None
        self._notification_event: asyncio.Event = asyncio.Event()
        self._last_status: dict[str, Any] | None = None
        self._cached_secret: list[int] | None = (
            list(cached_secret) if cached_secret and len(cached_secret) == 8 else None
        )
        self._secret_persist_cb = secret_persist_cb
        self._connect_grace_seconds = connect_grace_seconds
        # Per-fountain lock prevents simultaneous connect/handshake/poll/write
        # from interleaving on the same physical device. (PR #203 polish port
        # from aavdberg/ha-petkit v1.2.0.)
        self._lock: asyncio.Lock = asyncio.Lock()
        # Attach/configure the log file based on the user's debug toggle.
        _setup_ble_log_file(hass.config.config_dir, debug_enabled=debug_log)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def async_get_status(self) -> dict[str, Any] | None:
        """Connect to the fountain, read full status, disconnect.

        CTW3 / Eversweet Max 2 status reading strategy:
        1. Connect and run CMD 213 — capture proactive CMD 230 if it arrives
           within the 3 s window.
        2. If no proactive status: run auth (CMD 73/86/84).
        3. After auth: wait up to 10 s for the device to push CMD 230
           spontaneously (it sometimes does after a successful auth).
        4. For non-CTW3 models: poll CMD 230 explicitly after auth.

        Returns the parsed status dict, or None on failure.
        """
        async with self._lock:
            return await self._async_get_status_locked()

    async def _async_get_status_locked(self) -> dict[str, Any] | None:
        try:
            await self._connect()
            await self._run_init_sequence()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Init sequence incomplete for %s: %s (checking for captured status)",
                self.mac_address,
                err,
            )

        if self._last_status is not None:
            _LOGGER.debug("Using proactive status for %s", self.mac_address)
            await self._disconnect()
            return self._last_status

        if self._alias == "CTW3":
            return await self._async_get_ctw3_status()

        # Non-CTW3 models: poll status explicitly after auth (CMD 230).
        try:
            status = await self._read_full_status()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("BLE status read failed for %s: %s", self.mac_address, err)
            await self._disconnect()
            return None

        await self._disconnect()
        return status

    async def _async_get_ctw3_status(self) -> dict[str, Any] | None:
        """Poll status for CTW3 / Eversweet Max 2.

        Tries CMD 210 (primary), then CMD 211 (config merge), then CMD 230
        (fallback). Disconnects before returning in all cases.
        """
        # Primary poll: CMD 210 (device state).
        state_cmd = self._protocol.get_device_state_command()
        _LOGGER.debug(
            "CTW3 polling CMD 210 (state) for %s: %s",
            self.mac_address,
            state_cmd.hex(),
        )
        self._notification_event.clear()
        try:
            await self._write(state_cmd)
        except RuntimeError as err:
            _LOGGER.warning("CTW3 CMD 210 write failed: %s", err)
        else:
            with contextlib.suppress(asyncio.TimeoutError):
                async with asyncio.timeout(5.0):
                    await self._async_poll_for_status()

        # If state arrived, also fetch config (CMD 211) to merge LED/DND fields.
        if self._last_status is not None:
            await self._async_poll_config_cmd211()

        # Fallback: CMD 230 if CMD 210 gave no response.
        if self._last_status is None:
            await self._async_ctw3_fallback_cmd230()

        if self._last_status is not None:
            _LOGGER.debug("CTW3 status received for %s", self.mac_address)
            await self._disconnect()
            return self._last_status

        _LOGGER.warning(
            "No status received for CTW3 %s after CMD 210 + CMD 230",
            self.mac_address,
        )
        await self._log_readable_gatt_chars()
        await self._disconnect()
        return None

    async def _async_poll_for_status(self) -> None:
        """Wait for a status notification, looping with 1-second sub-intervals.

        Callers must wrap this coroutine with ``asyncio.timeout()`` to set
        the overall deadline, e.g.::

            async with asyncio.timeout(5.0):
                await self._async_poll_for_status()
        """
        while self._last_status is None:
            self._notification_event.clear()
            try:
                async with asyncio.timeout(1.0):
                    await self._notification_event.wait()
            except asyncio.TimeoutError:
                continue

    async def _async_poll_config_cmd211(self) -> None:
        """Send CMD 211 (config) and wait for any notification to arrive."""
        config_cmd = self._protocol.get_device_config_command()
        _LOGGER.debug(
            "CTW3 polling CMD 211 (config) for %s: %s",
            self.mac_address,
            config_cmd.hex(),
        )
        self._notification_event.clear()
        try:
            await self._write(config_cmd)
        except RuntimeError as err:
            _LOGGER.warning("CTW3 CMD 211 write failed: %s", err)
            return
        deadline = asyncio.get_running_loop().time() + 3.0
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            self._notification_event.clear()
            try:
                async with asyncio.timeout(min(remaining, 1.0)):
                    await self._notification_event.wait()
            except asyncio.TimeoutError:
                continue
            break  # any notification received — move on

    async def _async_ctw3_fallback_cmd230(self) -> None:
        """Send CMD 230 (type=2) as a fallback when CMD 210 gave no response."""
        status_cmd = self._protocol.get_status_command()
        _LOGGER.warning(
            "CTW3 CMD 210 no response — trying CMD 230 (type=2) for %s: %s",
            self.mac_address,
            status_cmd.hex(),
        )
        self._notification_event.clear()
        try:
            await self._write(status_cmd)
        except RuntimeError as err:
            _LOGGER.warning("CTW3 CMD 230 write failed: %s", err)
            return
        with contextlib.suppress(asyncio.TimeoutError):
            async with asyncio.timeout(5.0):
                await self._async_poll_for_status()

    async def async_set_mode(self, power_state: int, mode: int) -> bool:
        """Connect, send a mode change command, disconnect."""
        async with self._lock:
            try:
                await self._connect()
                await self._run_init_sequence()
                cmd = self._protocol.build_set_mode_command(power_state, mode)
                await self._write(cmd)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("BLE set_mode failed for %s: %s", self.mac_address, err)
                return False
            finally:
                await self._disconnect()
            return True

    async def async_set_config(self, config_data: list[int]) -> bool:
        """Connect, send a config update command, disconnect."""
        async with self._lock:
            try:
                await self._connect()
                await self._run_init_sequence()
                cmd = self._protocol.build_set_config_command(config_data)
                await self._write(cmd)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("BLE set_config failed for %s: %s", self.mac_address, err)
                return False
            finally:
                await self._disconnect()
            return True

    async def async_reset_filter(self) -> bool:
        """Connect, send filter reset command (CMD 222), disconnect."""
        async with self._lock:
            try:
                await self._connect()
                await self._run_init_sequence()
                cmd = self._protocol.get_reset_filter_command()
                await self._write(cmd)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(
                    "BLE reset_filter failed for %s: %s", self.mac_address, err
                )
                return False
            finally:
                await self._disconnect()
            return True

    # -----------------------------------------------------------------------
    # Connection management
    # -----------------------------------------------------------------------

    async def _connect(self) -> None:
        """Establish a BLE connection using HA bluetooth + bleak_retry_connector."""
        from bleak import BleakClient
        from bleak_retry_connector import establish_connection

        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.mac_address, connectable=True
        )
        # 15 s grace window: HA proxies / USB adapters may not have advertised
        # the device yet right after restart. Retry briefly before giving up.
        # (Polish port from aavdberg/ha-petkit v1.2.0.)
        if ble_device is None and self._connect_grace_seconds > 0:
            grace_deadline = (
                asyncio.get_running_loop().time() + self._connect_grace_seconds
            )
            _LOGGER.debug(
                "BLE device %s not yet advertised — waiting up to %.0fs",
                self.mac_address,
                self._connect_grace_seconds,
            )
            while ble_device is None:
                if asyncio.get_running_loop().time() >= grace_deadline:
                    break
                await asyncio.sleep(1.0)
                ble_device = bluetooth.async_ble_device_from_address(
                    self.hass, self.mac_address, connectable=True
                )
        if ble_device is None:
            raise RuntimeError(
                f"BLE device {self.mac_address} not found in HA scanner. "
                "Ensure Bluetooth is enabled and the fountain is powered on."
            )

        _LOGGER.debug(
            "BLE device found: %s via %s", ble_device.name, ble_device.details
        )
        # Log advertisement service_data — available without connecting; may encode status.
        svc_info: BluetoothServiceInfoBleak | None = bluetooth.async_last_service_info(
            self.hass, self.mac_address, connectable=False
        )
        if svc_info is not None and svc_info.advertisement.service_data:
            for uuid, raw in svc_info.advertisement.service_data.items():
                _LOGGER.debug(
                    "BLE adv service_data UUID=%s  hex=%s  dec=%s",
                    uuid,
                    raw.hex(),
                    list(raw),
                )
        # Derive the device alias from the *actual* BLE advertisement name so that the
        # correct protocol parser is used regardless of the user's friendly name in config.
        # e.g. "Petkit_CTW3_100" → alias "CTW3", "Petkit_W5_XYZ" → alias "W5"
        if ble_device.name:
            self._alias = _alias_from_name(ble_device.name)
        _LOGGER.debug(
            "BLE alias derived from device name '%s': '%s'",
            ble_device.name,
            self._alias,
        )
        mac_bytes = [int(b, 16) for b in self.mac_address.split(":")]
        self._protocol = LocalFountainBleProtocol(self._alias, mac_bytes=mac_bytes)
        self._last_status = None

        self._client = await establish_connection(
            BleakClient,
            ble_device,
            self.device_name,
            disconnected_callback=self._on_disconnected,
        )
        _LOGGER.info("BLE connected to %s (%s)", self.device_name, self.mac_address)
        await self._client.start_notify(BLE_NOTIFY_UUID, self._on_notification)
        _LOGGER.debug(
            "BLE notify started for %s (%s)", self.device_name, self.mac_address
        )

    async def _log_readable_gatt_chars(self) -> None:
        """Enumerate all GATT characteristics and read any readable ones.

        Called as a diagnostic when CTW3 fails to send CMD 230 via the
        notify channel. Logs each char UUID + raw value so we can discover
        an alternative status source.
        """
        if self._client is None or not self._client.is_connected:
            return
        try:
            services = self._client.services
            for service in services:
                for char in service.characteristics:
                    if "read" in char.properties:
                        try:
                            raw = await self._client.read_gatt_char(char.uuid)
                            _LOGGER.debug(
                                "GATT char %s (service %s): %s  dec=%s",
                                char.uuid,
                                service.uuid,
                                raw.hex(),
                                list(raw),
                            )
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.warning(
                                "GATT char %s read error: %s", char.uuid, err
                            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("GATT char enumeration failed: %s", err)

    async def _disconnect(self) -> None:
        if self._client and self._client.is_connected:
            try:
                await self._client.stop_notify(BLE_NOTIFY_UUID)
                await self._client.disconnect()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("BLE disconnect error for %s: %s", self.mac_address, err)
        self._client = None

    def _on_disconnected(self, _client: Any) -> None:
        _LOGGER.debug("BLE disconnected from %s", self.mac_address)

    # -----------------------------------------------------------------------
    # Protocol sequence
    # -----------------------------------------------------------------------

    async def _run_init_sequence(self) -> None:
        """Run the BLE initialization handshake.

        CTW3 / Eversweet Max 2 behaviour:
        - May send a proactive CMD 230 status frame ~200–400 ms after CMD 213
          (before any auth).  We extend the wait to 3 s to capture this.
        - Sending CMD 230 BEFORE auth causes the device to disconnect — do NOT
          do that.
        - After successful auth (CMD 73/86/84) the device may push a CMD 230
          spontaneously; async_get_status handles that with a passive wait.
        - If auth is rejected (CMD 86 response 00 or write error), stop
          gracefully so any already-captured status is preserved.
        """
        # Step 1: Send CMD 213 (request device ID); clear event first to avoid
        # racing against a fast response.
        self._notification_event.clear()
        for cmd in self._protocol.get_init_commands():
            _LOGGER.debug("BLE sending CMD 213 to %s: %s", self.mac_address, cmd.hex())
            await self._write(cmd)

        # Wait for the CMD 213 response. Only CTW3 / Eversweet Max 2 needs the
        # extra passive window to catch a proactive CMD 230 before auth; keep
        # the init path short for other models.
        with contextlib.suppress(asyncio.TimeoutError):
            async with asyncio.timeout(5.0):
                await self._wait_for_notification()
        protocol_model = (
            str(
                getattr(self._protocol, "model", None)
                or getattr(self._protocol, "device_model", None)
                or getattr(self._protocol, "product_name", None)
                or ""
            )
        ).upper()
        expects_proactive_status = "CTW3" in protocol_model or "MAX 2" in protocol_model
        if expects_proactive_status:
            await asyncio.sleep(3.0)

        if not self._protocol.device_id_received:
            _LOGGER.warning(
                "Device ID not received for %s; proceeding anyway", self.mac_address
            )
        else:
            _LOGGER.debug("Device ID received for %s", self.mac_address)

        if self._last_status is not None:
            _LOGGER.debug(
                "CTW3 proactive status captured during wait for %s — skipping auth",
                self.mac_address,
            )
            return

        # Step 2: Auth (CMD 73/86/84).  If the device disconnects mid-sequence,
        # catch the error and return — the caller will check _last_status.
        auth_cmds = self._protocol.complete_init_commands(
            secret_override=self._cached_secret
        )
        for cmd in auth_cmds:
            _LOGGER.debug("BLE sending init cmd to %s: %s", self.mac_address, cmd.hex())
            self._notification_event.clear()
            try:
                await self._write(cmd)
            except RuntimeError:
                _LOGGER.warning(
                    "Auth write failed for %s — device likely disconnected",
                    self.mac_address,
                )
                return
            await asyncio.sleep(BLE_CMD_DELAY)

        # Auth completed without write errors → persist the secret the device
        # accepted so future BLE sessions reuse it instead of re-deriving from
        # CMD 213 (preserves cloud-relay session). Jezza34000 PR #203 design.
        if self._secret_persist_cb is not None:
            try:
                accepted = self._protocol.current_secret
                if accepted and accepted != self._cached_secret:
                    self._cached_secret = list(accepted)
                    secret_hex = bytes(accepted).hex()
                    _LOGGER.debug(
                        "Caching BLE secret for %s: %s",
                        self.mac_address,
                        secret_hex,
                    )
                    res = self._secret_persist_cb(self.mac_address, secret_hex)
                    if asyncio.iscoroutine(res):
                        await res
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "BLE secret persist callback failed for %s: %s",
                    self.mac_address,
                    err,
                )

    async def _read_full_status(self) -> dict[str, Any] | None:
        """Send CMD 230 and wait for the status notification.

        Uses a loop to specifically wait for a CMD-230 response, ignoring any
        delayed notification responses from earlier init commands (e.g. CMD 84).
        """
        # Brief pause to let any delayed init-sequence responses arrive and clear.
        await asyncio.sleep(0.5)

        self._last_status = None
        self._notification_event.clear()
        status_cmd = self._protocol.get_status_command()
        _LOGGER.debug(
            "BLE sending CMD 230 to %s: %s", self.mac_address, status_cmd.hex()
        )
        await self._write(status_cmd)

        # Loop until we receive a CMD-230 notification or the deadline expires.
        deadline = asyncio.get_running_loop().time() + BLE_NOTIFY_TIMEOUT
        while self._last_status is None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                _LOGGER.warning(
                    "BLE status timeout for %s (no CMD 230 response received)",
                    self.mac_address,
                )
                break
            self._notification_event.clear()
            try:
                async with asyncio.timeout(min(remaining, 2.0)):
                    await self._notification_event.wait()
            except asyncio.TimeoutError:
                continue  # keep looping until deadline

        return self._last_status

    # -----------------------------------------------------------------------
    # BLE I/O helpers
    # -----------------------------------------------------------------------

    async def _write(self, data: bytearray) -> None:
        if self._client is None or not self._client.is_connected:
            raise RuntimeError("BLE client is not connected")
        await self._client.write_gatt_char(BLE_WRITE_UUID, data, response=False)

    async def _wait_for_notification(self) -> None:
        """Wait for a BLE notification (no internal timeout).

        The caller must clear ``_notification_event`` **before** sending the
        BLE write command to avoid a race condition where the device responds
        faster than this method is reached.

        Wrap with ``asyncio.timeout()`` at the call site to bound the wait::

            async with asyncio.timeout(5.0):
                await self._wait_for_notification()
        """
        await self._notification_event.wait()

    def _on_notification(self, _sender: Any, data: bytearray) -> None:
        """Handle incoming BLE notification."""
        result = self._protocol.handle_notification(data)
        if result is not None:
            if result.get("_config_only"):
                # CMD 211 config — merge into existing status rather than replace.
                result = {k: v for k, v in result.items() if k != "_config_only"}
                if self._last_status is not None:
                    self._last_status.update(result)
            else:
                self._last_status = result
        # Signal that at least one notification arrived
        self._notification_event.set()
