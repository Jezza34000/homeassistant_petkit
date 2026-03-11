"""Petkit IoT MQTT listener

The official Petkit mobile app connects to an MQTT broker (Aliyun-style topics) to
receive near real-time device events. We use those messages as a trigger to refresh
data from the REST API, providing faster state updates in Home Assistant without
having to reverse-engineer every message type.
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
import hashlib
import hmac
import json
import logging
import re
from typing import Any

from pypetkitapi.client import PetKitClient

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DEFAULT_SCAN_INTERVAL, SCAN_INTERVAL_SLOW
from .coordinator import PetkitDataUpdateCoordinator

LOGGER = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    mqtt = None
    CallbackAPIVersion = None


_HOST_PORT_RE = re.compile(r"^(?P<host>.+?)(?::(?P<port>\d+))?$")
_SCHEME_RE = re.compile(r"^(?:tcp|ssl|mqtt|mqtts)://", re.IGNORECASE)


class MqttConnectionStatus(StrEnum):
    """MQTT connection status."""

    NOT_STARTED = "not_started"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    FAILED = "failed"


@dataclass(frozen=True)
class _MqttEndpoint:
    host: str
    port: int


@dataclass
class MqttInnerContent:
    """Parsed inner `contentAsString` JSON."""

    inner_type: int | str | None = None
    snapshot: dict[str, Any] | None = None
    content: Any = None
    payload: Any = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MqttPayload:
    """Parsed `NewMessage` payload."""

    content_as_string: str | None = None
    from_field: str | None = None
    to: str | None = None
    time: int | None = None
    timestamp: int | None = None
    inner: MqttInnerContent | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedIoTMessage:
    """Top-level parsed IoT message."""

    device_name: str | None = None
    timestamp: int | None = None
    message_type: str | None = None
    payload: MqttPayload | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _aliyun_mqtt_sign(
    product_key: str,
    device_name: str,
    device_secret: str,
    client_id: str,
) -> tuple[str, str, str]:
    """Compute Aliyun IoT MQTT credentials (clientId, username, password).

    Follows the Alibaba Cloud IoT authentication protocol:
    - clientId: ``{clientId}|securemode=3,signmethod=hmacsha256|``
    - username: ``{deviceName}&{productKey}``
    - password: HMAC-SHA256(deviceSecret, content) where content is
      ``clientId{cid}deviceName{dn}productKey{pk}`` (keys sorted alphabetically).
    """
    content = f"clientId{client_id}deviceName{device_name}productKey{product_key}"
    sign = hmac.new(
        device_secret.encode(), content.encode(), hashlib.sha256
    ).hexdigest()
    mqtt_client_id = f"{client_id}|securemode=3,signmethod=hmacsha256|"
    mqtt_username = f"{device_name}&{product_key}"
    return mqtt_client_id, mqtt_username, sign


def _parse_mqtt_host(raw: str, *, default_port: int = 1883) -> _MqttEndpoint:
    """Parse `host[:port]` as returned by Petkit's IoT endpoint."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Empty mqtt host")

    # Strip URI scheme prefixes (tcp://, ssl://, mqtt://, mqtts://)
    raw = _SCHEME_RE.sub("", raw)

    # Very small parser; Petkit appears to return host:port for IPv4/hostname.
    # If we ever encounter IPv6 literals, we can extend this to handle [::1]:1883.
    match = _HOST_PORT_RE.match(raw)
    if not match:
        raise ValueError(f"Invalid mqtt host: {raw!r}")

    host = (match.group("host") or "").strip()
    port_raw = match.group("port")
    port = int(port_raw) if port_raw else default_port

    if not host:
        raise ValueError(f"Invalid mqtt host: {raw!r}")
    return _MqttEndpoint(host=host, port=port)


def _parse_inner_content(text: str | None) -> MqttInnerContent | None:
    """Parse the inner `contentAsString` JSON payload."""
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return MqttInnerContent(
        inner_type=data.get("type"),
        snapshot=(
            data.get("snapshot") if isinstance(data.get("snapshot"), dict) else None
        ),
        content=data.get("content"),
        payload=data.get("payload"),
        raw=data,
    )


def _parse_iot_message(payload_text: str) -> ParsedIoTMessage | None:
    """Parse a full IoT MQTT message from its JSON text."""
    try:
        data = json.loads(payload_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    # Parse the nested NewMessage payload
    raw_payload = data.get("payload")
    mqtt_payload: MqttPayload | None = None
    if isinstance(raw_payload, dict):
        content_str = raw_payload.get("contentAsString")
        # "from" and "to" can be either strings or {"username": "..."}
        raw_from = raw_payload.get("from")
        raw_to = raw_payload.get("to")
        from_str = raw_from.get("username") if isinstance(raw_from, dict) else raw_from
        to_str = raw_to.get("username") if isinstance(raw_to, dict) else raw_to
        mqtt_payload = MqttPayload(
            content_as_string=content_str,
            from_field=from_str,
            to=to_str,
            time=raw_payload.get("time"),
            timestamp=raw_payload.get("timestamp"),
            inner=_parse_inner_content(content_str),
            raw=raw_payload,
        )

    return ParsedIoTMessage(
        device_name=data.get("deviceName"),
        timestamp=data.get("timestamp"),
        message_type=data.get("type"),
        payload=mqtt_payload,
        raw=data,
    )


class PetkitIotMqttListener:
    """Connect to Petkit's MQTT broker and refresh HA data on events."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PetKitClient,
        coordinator: PetkitDataUpdateCoordinator,
        *,
        refresh_debounce_s: float = 0.5,
    ) -> None:
        """Init PetkitIotMqttListener"""
        self.hass = hass
        self.client = client
        self.coordinator = coordinator
        self.refresh_debounce_s = refresh_debounce_s

        self._mqtt_client = None
        self._subscribe_topics: list[str] = []
        self._refresh_task: asyncio.Task | None = None
        self._started = False
        self._petkit_device_name: str | None = None
        self._petkit_product_key: str | None = None
        self._recent_messages: deque[dict] = deque(maxlen=200)

        # Connection status tracking
        self._connection_status = MqttConnectionStatus.NOT_STARTED
        self._messages_received: int = 0
        self._last_message_at: datetime | None = None
        self._first_message_logged = False

    @property
    def connection_status(self) -> MqttConnectionStatus:
        """Return current MQTT connection status."""
        return self._connection_status

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return diagnostic info about the MQTT connection."""
        return {
            "status": self._connection_status.value,
            "messages_received": self._messages_received,
            "last_message_at": (
                self._last_message_at.isoformat() if self._last_message_at else None
            ),
            "buffer_size": len(self._recent_messages),
            "topics": list(self._subscribe_topics),
        }

    async def async_start(self) -> None:
        """Start the MQTT connection in the background."""
        if self._started:
            return
        self._started = True

        if mqtt is None:  # pragma: no cover
            LOGGER.error("paho-mqtt not installed; Petkit MQTT listener cannot start")
            self._connection_status = MqttConnectionStatus.FAILED
            return

        try:
            iot = await self.client.get_iot_mqtt_config()
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("Failed to fetch Petkit IoT MQTT config: %s", err)
            self._connection_status = MqttConnectionStatus.FAILED
            return

        if not (
            iot.mqtt_host and iot.device_name and iot.device_secret and iot.product_key
        ):
            LOGGER.warning("Config is missing required fields; listener disabled")
            self._connection_status = MqttConnectionStatus.FAILED
            return

        try:
            endpoint = _parse_mqtt_host(iot.mqtt_host)
        except ValueError as err:
            LOGGER.warning("Invalid host %r: %s", iot.mqtt_host, err)
            self._connection_status = MqttConnectionStatus.FAILED
            return

        self._petkit_device_name = iot.device_name
        self._petkit_product_key = iot.product_key
        base = f"/{iot.product_key}/{iot.device_name}/user"
        # The official Android app only subscribes to /user/get.
        self._subscribe_topics = [f"{base}/get"]

        # Aliyun IoT MQTT requires HMAC-signed credentials.
        mqtt_client_id, mqtt_username, mqtt_password = _aliyun_mqtt_sign(
            product_key=iot.product_key,
            device_name=iot.device_name,
            device_secret=iot.device_secret,
            client_id=iot.device_name,
        )
        paho_client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=mqtt_client_id,
            clean_session=False,
            protocol=mqtt.MQTTv311,
        )
        paho_client.username_pw_set(mqtt_username, mqtt_password)
        paho_client.will_set(
            f"{base}/update", payload='{"status":"offline"}', qos=0, retain=False
        )
        paho_client.reconnect_delay_set(min_delay=1, max_delay=30)

        paho_client.on_connect = self._on_connect
        paho_client.on_disconnect = self._on_disconnect
        paho_client.on_message = self._on_message

        self._mqtt_client = paho_client
        self._connection_status = MqttConnectionStatus.CONNECTING

        try:
            paho_client.connect_async(endpoint.host, endpoint.port, keepalive=60)
            paho_client.loop_start()
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("Failed to start connection: %s", err)
            self._mqtt_client = None
            self._subscribe_topics = []
            self._connection_status = MqttConnectionStatus.FAILED
            return

        LOGGER.debug(
            "Listener started (broker=%s:%s, topics=%s)",
            endpoint.host,
            endpoint.port,
            self._subscribe_topics,
        )

    async def async_stop(self) -> None:
        """Stop the MQTT listener and cleanup resources."""
        self._started = False

        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None

        client = self._mqtt_client
        self._mqtt_client = None
        self._subscribe_topics = []
        self._petkit_device_name = None
        self._petkit_product_key = None

        if client is None:
            return

        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Disconnect raised", exc_info=True)

        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Loop_stop raised", exc_info=True)

        self._connection_status = MqttConnectionStatus.DISCONNECTED
        LOGGER.debug("Listener stopped")

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        """Handle connect callback."""
        topics = self._subscribe_topics
        if reason_code != 0:
            LOGGER.warning("connect failed (reason_code=%s)", reason_code)
            self._connection_status = MqttConnectionStatus.FAILED
            return
        self._connection_status = MqttConnectionStatus.CONNECTED
        self.hass.loop.call_soon_threadsafe(
            self._set_polling_interval, SCAN_INTERVAL_SLOW
        )
        self.hass.loop.call_soon_threadsafe(self._update_coordinator_mqtt_state, True)
        if not topics:
            LOGGER.warning("connected but subscribe topics are missing")
            return
        try:
            for topic in topics:
                client.subscribe(topic, qos=0)
            LOGGER.info("Subscribed to %s", topics)
        except Exception:  # noqa: BLE001
            LOGGER.warning("Subscribe failed", exc_info=True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        """Handle a MQTT disconnect."""
        if reason_code != 0:
            LOGGER.warning("Disconnected unexpectedly (reason_code=%s)", reason_code)
            self._connection_status = MqttConnectionStatus.DISCONNECTED
        else:
            LOGGER.info("Disconnected")
            self._connection_status = MqttConnectionStatus.DISCONNECTED
        self.hass.loop.call_soon_threadsafe(
            self._set_polling_interval, DEFAULT_SCAN_INTERVAL
        )
        self.hass.loop.call_soon_threadsafe(self._update_coordinator_mqtt_state, False)

    def _on_message(self, client, userdata, message) -> None:
        """Handle a MQTT message."""
        topic = getattr(message, "topic", None)
        payload = getattr(message, "payload", b"")
        self.hass.add_job(self.async_handle_message, topic, payload)

    async def async_handle_message(self, topic: str | None, payload: bytes) -> None:
        """Handle an incoming MQTT message on the HA event loop thread."""
        self._messages_received += 1
        self._last_message_at = dt_util.utcnow()

        payload_encoding = "utf-8"
        try:
            payload_text = payload.decode("utf-8")
        except UnicodeDecodeError:
            payload_text = base64.b64encode(payload).decode("ascii")
            payload_encoding = "base64"

        # Parse structured message data
        inner_type_str = None
        parsed: ParsedIoTMessage | None = None
        if payload_encoding == "utf-8":
            parsed = await self.hass.async_add_executor_job(
                _parse_iot_message, payload_text
            )

        # Log every message at INFO with useful detail
        if parsed and parsed.payload and parsed.payload.inner:
            inner_type_str = parsed.payload.inner.inner_type
            LOGGER.debug(
                "Received: topic=%s type=%s inner=%s from=%s",
                topic,
                parsed.message_type,
                inner_type_str,
                parsed.payload.from_field,
            )
        else:
            LOGGER.debug(
                "Received: topic=%s (%d bytes)",
                topic,
                len(payload),
            )

        event_data: dict[str, Any] = {
            "topic": topic or "",
            "payload": payload_text,
            "payload_encoding": payload_encoding,
            "received_at": self._last_message_at.isoformat(),
            "petkit_device_name": self._petkit_device_name or "",
            "petkit_product_key": self._petkit_product_key or "",
        }

        if parsed is not None:
            event_data["message_type"] = parsed.message_type
            event_data["source_device"] = parsed.device_name
            if parsed.payload and parsed.payload.inner:
                event_data["inner_type"] = inner_type_str

        self._recent_messages.append(event_data)
        self.hass.bus.async_fire("petkit_mqtt_message", event_data)

        self._schedule_refresh()

    def get_recent_messages(
        self, *, limit: int = 1, topic_contains: str | None = None
    ) -> list[dict]:
        """Return up to `limit` most recent messages, optionally filtered by topic substring."""
        msgs = list(self._recent_messages)
        if topic_contains:
            msgs = [m for m in msgs if topic_contains in m.get("topic", "")]
        if limit <= 0:
            return []
        return msgs[-limit:]

    def _schedule_refresh(self) -> None:
        """Schedule refresh."""
        if self._refresh_task is not None and not self._refresh_task.done():
            LOGGER.debug("Refresh task is already running.")
            return
        self._refresh_task = self.hass.async_create_task(self._debounced_refresh())

    async def _debounced_refresh(self) -> None:
        """Debounce the refresh."""
        await asyncio.sleep(self.refresh_debounce_s)
        try:
            await self.coordinator.async_request_refresh()
        except Exception:  # noqa: BLE001
            LOGGER.warning("MQTT-triggered refresh failed", exc_info=True)

    def _update_coordinator_mqtt_state(self, is_connected: bool) -> None:
        """Update the mqtt flag inside coordinateur."""
        self.coordinator.mqtt_connected = is_connected

    def _set_polling_interval(self, seconds: int) -> None:
        """Modify coordinator refresh interval"""
        self.coordinator.update_interval = timedelta(seconds=seconds)
        LOGGER.debug("Change coordinator refresh interval to %s sec", seconds)
