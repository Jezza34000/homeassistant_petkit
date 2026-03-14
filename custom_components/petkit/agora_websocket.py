"""Agora WebSocket signaling for PetKit WebRTC streaming."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass
import json
import logging
import secrets
import ssl
import time
from typing import Any

from pypetkitapi import LiveFeed
from sdp_transform import parse as sdp_parse
from webrtc_models import RTCIceCandidateInit
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from .agora_api import RESPONSE_FLAGS, AgoraResponse
from .agora_sdp import parse_offer_to_ortc

LOGGER = logging.getLogger(__name__)


def _create_ws_ssl_context() -> ssl.SSLContext:
    """Create a secure SSL context for Agora edge WebSocket."""
    ssl_context = ssl.create_default_context()
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2

    return ssl_context


_SSL_CONTEXT = _create_ws_ssl_context()


@dataclass
class OfferSdpInfo:
    """Selected pieces of the browser offer SDP used for answer generation."""

    parsed_sdp: dict[str, Any]
    fingerprint: str
    ice_ufrag: str
    ice_pwd: str
    audio_extensions: list[dict[str, Any]]
    video_extensions: list[dict[str, Any]]
    audio_direction: str
    video_direction: str
    extmap_allow_mixed: bool
    setup_role: str


class AgoraWebSocketHandler:
    """WebSocket handler for Agora join_v3 signaling."""

    def __init__(
        self,
        rtc_token_provider: Callable[[], Awaitable[str | None]] | None = None,
        *,
        prefer_instant_video: bool = False,
        subscribe_retry_delay: float = 0.0,
        subscribe_retry_attempts: int = 0,
    ) -> None:
        """Initialize runtime state."""
        self._websocket: ClientConnection | None = None
        self._connection_state = "DISCONNECTED"
        self._message_handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._disconnect_task: asyncio.Task[None] | None = None

        self.candidates: list[RTCIceCandidateInit] = []
        self._online_users: set[int] = set()
        self._video_streams: dict[int, dict[str, Any]] = {}
        self._subscribed_video_streams: set[tuple[int, int]] = set()

        self._message_loop_task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._subscribe_retry_task: asyncio.Task[None] | None = None

        self._joined = False
        self._answer_sdp: str | None = None
        self._rtc_token: str | None = None
        self._rtc_token_provider = rtc_token_provider
        self._prefer_instant_video = prefer_instant_video
        self._subscribe_retry_delay = subscribe_retry_delay
        self._subscribe_retry_attempts = subscribe_retry_attempts

        self._setup_message_handlers()

    def _setup_message_handlers(self) -> None:
        """Register incoming message handlers."""
        self._message_handlers = {
            "answer": self._handle_answer,
            "on_p2p_lost": self._handle_p2p_lost,
            "error": self._handle_error,
            "on_rtp_capability_change": self._handle_rtp_capability_change,
            "on_user_online": self._handle_user_online,
            "on_add_video_stream": self._handle_add_video_stream,
        }

    def add_ice_candidate(self, candidate: RTCIceCandidateInit) -> None:
        """Collect browser ICE candidates before join_v3."""
        self.candidates.append(candidate)

    async def connect_and_join(
        self,
        live_feed: LiveFeed,
        offer_sdp: str,
        session_id: str,
        app_id: str,
        agora_response: AgoraResponse,
    ) -> str | None:
        """Connect to Agora edge WebSocket and return answer SDP."""
        self._rtc_token = live_feed.rtc_token

        offer_info = self._parse_offer_sdp(offer_sdp)
        if offer_info is None:
            LOGGER.error("Failed to parse offer SDP")
            return None

        ortc_info = parse_offer_to_ortc(offer_sdp)
        if not ortc_info:
            LOGGER.error("Failed to build ORTC capabilities from offer")
            return None

        # Add gathered candidates to ORTC offer before join_v3.
        gathered_candidates = self._convert_candidates_to_ortc()
        if gathered_candidates:
            ortc_info.setdefault("iceParameters", {})[
                "candidates"
            ] = gathered_candidates
        LOGGER.debug(
            "Agora join_v3: session=%s gathered_candidates=%d",
            session_id,
            len(gathered_candidates),
        )

        gateway_addresses = agora_response.get_gateway_addresses()
        if not gateway_addresses:
            LOGGER.warning(
                "No gateway addresses in flag 4096; using fallback addresses"
            )
            gateway_addresses = agora_response.addresses

        LOGGER.debug(
            "Agora join_v3: trying %d gateway addresses",
            len(gateway_addresses),
        )
        for gateway in gateway_addresses:
            edge_ip_dashed = gateway.ip.replace(".", "-")
            ws_url = f"wss://{edge_ip_dashed}.edge.agora.io:{gateway.port}"

            try:
                async with asyncio.timeout(10):
                    websocket = await connect(
                        ws_url,
                        ssl=_SSL_CONTEXT,
                        ping_timeout=30,
                        close_timeout=30,
                    )

                self._websocket = websocket
                self._connection_state = "CONNECTED"
                LOGGER.info("Connected to Agora WebSocket: %s", ws_url)

                join_message = self._create_join_message(
                    live_feed=live_feed,
                    session_id=session_id,
                    app_id=app_id,
                    ortc_info=ortc_info,
                    agora_response=agora_response,
                )
                await websocket.send(json.dumps(join_message))
                LOGGER.debug("Sent join_v3 message")

                answer_sdp = await self._wait_for_join_response(
                    websocket=websocket,
                    offer_info=offer_info,
                    agora_response=agora_response,
                )

                if answer_sdp:
                    self._message_loop_task = asyncio.create_task(
                        self._message_loop(websocket)
                    )
                    self._ping_task = asyncio.create_task(self._ping_loop())
                    if self._subscribe_retry_attempts > 0:
                        self._subscribe_retry_task = asyncio.create_task(
                            self._subscribe_retry_loop()
                        )
                    return answer_sdp

                await websocket.close()
                self._websocket = None

            except asyncio.TimeoutError:
                LOGGER.warning("WebSocket connection timeout for %s", ws_url)
                await self.disconnect()
                continue
            except (WebSocketException, json.JSONDecodeError, OSError) as err:
                LOGGER.warning("WebSocket signaling failed for %s: %s", ws_url, err)
                await self.disconnect()
                continue

        LOGGER.error("Failed to negotiate with all Agora edge gateways")
        self._connection_state = "DISCONNECTED"
        return None

    async def _wait_for_join_response(
        self,
        websocket: ClientConnection,
        offer_info: OfferSdpInfo,
        agora_response: AgoraResponse,
    ) -> str | None:
        """Wait for join success / answer after join_v3."""
        try:
            async with asyncio.timeout(15):
                async for raw_message in websocket:
                    try:
                        response = json.loads(raw_message)
                    except json.JSONDecodeError:
                        LOGGER.debug("Dropped non-JSON websocket payload")
                        continue

                    message_type = response.get("_type", "")
                    if message_type in self._message_handlers:
                        result = await self._message_handlers[message_type](response)
                        if isinstance(result, str) and result:
                            return result

                    if response.get("_result") == "success":
                        answer = await self._handle_join_success(
                            response=response,
                            offer_info=offer_info,
                            agora_response=agora_response,
                        )
                        if answer:
                            return answer

        except asyncio.TimeoutError:
            LOGGER.error("Timeout waiting for join_v3 response")
        except WebSocketException as err:
            LOGGER.error("WebSocket error while waiting for join response: %s", err)
            self._connection_state = "DISCONNECTED"

        return None

    async def _message_loop(self, websocket: ClientConnection) -> None:
        """Process messages after join success."""
        LOGGER.debug("Started Agora background message loop")
        try:
            async for raw_message in websocket:
                try:
                    response = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue

                message_type = response.get("_type", "")
                LOGGER.debug("WS ← [%s] %s", message_type, response)

                if message_type in self._message_handlers:
                    await self._message_handlers[message_type](response)

                if message_type == "on_token_privilege_will_expire":
                    LOGGER.warning("Agora token expiring soon, sending renew_token")
                    await self._send_renew_token()
                elif message_type == "on_token_privilege_did_expire":
                    LOGGER.error("Agora token expired")

        except asyncio.CancelledError:
            LOGGER.debug("Agora message loop cancelled")
            raise
        except WebSocketException as err:
            LOGGER.warning("Agora message loop closed: %s", err)
        finally:
            self._connection_state = "DISCONNECTED"

    async def _ping_loop(self) -> None:
        """Keep WebSocket session alive with ping messages."""
        try:
            while self._websocket and self._connection_state == "CONNECTED":
                await asyncio.sleep(3)
                if not self._websocket:
                    break
                ping_message = {
                    "_id": secrets.token_hex(3),
                    "_type": "ping",
                }
                await self._websocket.send(json.dumps(ping_message))
        except asyncio.CancelledError:
            LOGGER.debug("Agora ping loop cancelled")
            raise
        except (WebSocketException, OSError) as err:
            LOGGER.debug("Agora ping loop ended: %s", err)

    async def _send_renew_token(self) -> None:
        """Send renew_token with current rtc token."""
        if self._rtc_token_provider:
            try:
                refreshed_token = await self._rtc_token_provider()
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("Failed to refresh RTC token for renew_token: %s", err)
                return
            if not refreshed_token:
                LOGGER.debug(
                    "RTC token refresh returned empty value; skipping renew_token"
                )
                return
            self._rtc_token = refreshed_token

        if not self._websocket or not self._rtc_token:
            return

        renew_message = {
            "_id": secrets.token_hex(3),
            "_type": "renew_token",
            "_message": {"token": self._rtc_token},
        }
        await self._websocket.send(json.dumps(renew_message))

    async def _handle_join_success(
        self,
        response: dict[str, Any],
        offer_info: OfferSdpInfo,
        agora_response: AgoraResponse,
    ) -> str | None:
        """Handle join_v3 success and generate browser answer SDP."""
        message = response.get("_message", {})
        ortc = message.get("ortc", {})
        if not ortc:
            LOGGER.error("join_v3 success did not include ORTC parameters")
            return None

        await self._send_set_client_role(role="host", level=0)
        await self._register_existing_video_streams(message)

        # Inject auth fingerprints if not present in ORTC payload.
        dtls_parameters = ortc.setdefault("dtlsParameters", {})
        fingerprints = dtls_parameters.setdefault("fingerprints", [])

        seen = {
            str(item.get("fingerprint", "")).lower()
            for item in fingerprints
            if item.get("fingerprint")
        }

        gateway_addresses = (
            agora_response.get_gateway_addresses() or agora_response.addresses
        )
        for address in gateway_addresses:
            if not address.fingerprint:
                continue

            fingerprint_algorithm = "sha-256"
            fingerprint_value = address.fingerprint
            if " " in fingerprint_value:
                parts = fingerprint_value.split()
                if len(parts) == 2:
                    fingerprint_algorithm = parts[0]
                    fingerprint_value = parts[1]

            if fingerprint_value.lower() in seen:
                continue

            fingerprints.append(
                {
                    "hashFunction": fingerprint_algorithm,
                    "fingerprint": fingerprint_value,
                }
            )
            seen.add(fingerprint_value.lower())

        answer_sdp = self._generate_answer_sdp(ortc, offer_info)
        if answer_sdp:
            self._joined = True
            self._answer_sdp = answer_sdp
            return answer_sdp
        return None

    async def _handle_answer(self, response: dict[str, Any]) -> str | None:
        """Handle direct `answer` message containing SDP."""
        message = response.get("_message", {})
        answer_sdp = message.get("sdp")
        if answer_sdp:
            self._answer_sdp = answer_sdp
            return answer_sdp
        return None

    async def _handle_p2p_lost(self, response: dict[str, Any]) -> None:
        """Handle p2p_lost signaling."""
        LOGGER.warning(
            "Agora p2p_lost: code=%s error=%s",
            response.get("error_code"),
            response.get("error_str"),
        )
        self._disconnect_task = asyncio.create_task(self.disconnect())

    async def _handle_error(self, response: dict[str, Any]) -> None:
        """Handle generic Agora signaling errors."""
        message = response.get("_message", {})
        LOGGER.error("Agora error message: %s", message.get("error", message))

    async def _handle_rtp_capability_change(self, response: dict[str, Any]) -> None:
        """Handle capability updates."""
        LOGGER.debug("Agora rtp capability change: %s", response.get("_message", {}))

    async def _handle_user_online(self, response: dict[str, Any]) -> None:
        """Track online users."""
        message = response.get("_message", {})
        uid = message.get("uid")
        if isinstance(uid, int):
            self._online_users.add(uid)

    async def _handle_add_video_stream(self, response: dict[str, Any]) -> None:
        """Auto-subscribe to newly announced video stream."""
        message = response.get("_message", {})
        uid = message.get("uid")
        ssrc_id = message.get("ssrcId")
        rtx_ssrc_id = message.get("rtxSsrcId")
        cname = message.get("cname")
        is_video = bool(message.get("video"))

        if not isinstance(uid, int) or not is_video:
            return

        LOGGER.debug(
            "Agora on_add_video_stream: uid=%s ssrc=%s rtx_ssrc=%s",
            uid,
            ssrc_id,
            rtx_ssrc_id,
        )
        self._video_streams[uid] = {
            "ssrcId": ssrc_id,
            "rtxSsrcId": rtx_ssrc_id,
            "cname": cname,
        }

        if isinstance(ssrc_id, int):
            await self._subscribe_video_stream(uid=uid, ssrc_id=ssrc_id)

    async def _send_set_client_role(
        self, role: str = "audience", level: int = 1
    ) -> None:
        """Send set_client_role signaling message."""
        if not self._websocket:
            return

        message = {
            "_id": secrets.token_hex(3),
            "_type": "set_client_role",
            "_message": {
                "role": role,
                "level": level,
                "client_ts": int(time.time() * 1000),
            },
        }
        await self._websocket.send(json.dumps(message))

    async def _send_subscribe(
        self,
        stream_id: int,
        ssrc_id: int,
        codec: str = "h264",
        stream_type: str = "video",
        mode: str = "live",
        p2p_id: int = 1,
        twcc: bool = True,
        rtx: bool = True,
        extend: str = "",
    ) -> None:
        """Send subscribe message for one remote stream."""
        if not self._websocket:
            return

        LOGGER.debug(
            "Agora subscribe: stream_id=%s ssrc_id=%s codec=%s",
            stream_id,
            ssrc_id,
            codec,
        )
        message = {
            "_id": secrets.token_hex(3),
            "_type": "subscribe",
            "_message": {
                "stream_id": stream_id,
                "stream_type": stream_type,
                "mode": mode,
                "codec": codec,
                "p2p_id": p2p_id,
                "twcc": twcc,
                "rtx": rtx,
                "extend": extend,
                "ssrcId": ssrc_id,
            },
        }
        await self._websocket.send(json.dumps(message))
        self._subscribed_video_streams.add((stream_id, ssrc_id))

    async def _subscribe_video_stream(self, uid: int, ssrc_id: int) -> None:
        """Subscribe once per `(uid, ssrc_id)` pair."""
        if (uid, ssrc_id) in self._subscribed_video_streams:
            return
        await self._send_subscribe(stream_id=uid, ssrc_id=ssrc_id, codec="h264")

    async def _register_existing_video_streams(self, payload: Any) -> None:
        """Subscribe to any already-published video streams present in join payload."""
        streams = self._find_existing_video_streams(payload)
        if streams:
            LOGGER.debug(
                "Agora join_v3: found %d existing video streams in join payload",
                len(streams),
            )

        for uid, ssrc_id in streams:
            self._video_streams.setdefault(uid, {"ssrcId": ssrc_id})
            await self._subscribe_video_stream(uid=uid, ssrc_id=ssrc_id)

    @classmethod
    def _find_existing_video_streams(cls, payload: Any) -> list[tuple[int, int]]:
        """Walk a join payload and extract existing video stream descriptors."""
        found: list[tuple[int, int]] = []

        def _visit(node: Any) -> None:
            if isinstance(node, dict):
                stream = cls._extract_existing_video_stream(node)
                if stream is not None:
                    found.append(stream)

                for value in node.values():
                    _visit(value)
                return

            if isinstance(node, list):
                for item in node:
                    _visit(item)

        _visit(payload)

        deduped: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for stream in found:
            if stream in seen:
                continue
            seen.add(stream)
            deduped.append(stream)
        return deduped

    @staticmethod
    def _extract_existing_video_stream(node: dict[str, Any]) -> tuple[int, int] | None:
        """Extract one existing video stream descriptor when present."""
        uid = node.get("uid")
        ssrc_id = node.get("ssrcId")
        has_video_marker = (
            node.get("video") is True
            or node.get("stream_type") == "video"
            or node.get("type") == "video"
            or node.get("codec") in {"h264", "h265", "video"}
            or node.get("rtxSsrcId") is not None
        )
        if not has_video_marker:
            return None
        if not isinstance(uid, int) or not isinstance(ssrc_id, int):
            return None
        return (uid, ssrc_id)

    async def _subscribe_retry_loop(self) -> None:
        """Retry subscribe shortly after join for WHEP-style consumers."""
        try:
            for attempt in range(self._subscribe_retry_attempts):
                await asyncio.sleep(self._subscribe_retry_delay)
                if not self._websocket or self._connection_state != "CONNECTED":
                    return

                pending = [
                    (uid, data.get("ssrcId"))
                    for uid, data in self._video_streams.items()
                    if isinstance(data.get("ssrcId"), int)
                ]
                if not pending:
                    continue

                LOGGER.debug(
                    "Agora subscribe retry %d/%d: known_streams=%d",
                    attempt + 1,
                    self._subscribe_retry_attempts,
                    len(pending),
                )
                for uid, ssrc_id in pending:
                    await self._send_subscribe(
                        stream_id=uid,
                        ssrc_id=ssrc_id,
                        codec="h264",
                    )
        except asyncio.CancelledError:
            LOGGER.debug("Agora subscribe retry loop cancelled")
            raise

    def _create_join_message(
        self,
        live_feed: LiveFeed,
        session_id: str,
        app_id: str,
        ortc_info: dict[str, Any],
        agora_response: AgoraResponse,
    ) -> dict[str, Any]:
        """Build join_v3 message payload."""
        process_id = (
            f"process-{secrets.token_hex(4)}-{secrets.token_hex(2)}-"
            f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(6)}"
        )

        return {
            "_id": secrets.token_hex(3),
            "_type": "join_v3",
            "_message": {
                "p2p_id": 1,
                "session_id": session_id,
                "app_id": app_id,
                "channel_key": live_feed.rtc_token,
                "channel_name": live_feed.channel_id,
                "sdk_version": "4.24.0",
                "browser": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/142.0.0.0 Safari/537.36"
                ),
                "process_id": process_id,
                "mode": "live",
                "codec": "h264",
                "role": "host",
                "has_changed_gateway": False,
                "ap_response": agora_response.to_ap_response(
                    RESPONSE_FLAGS["CHOOSE_SERVER"]
                ),
                "extend": "",
                "details": {},
                "features": {"rejoin": True},
                "attributes": {
                    "userAttributes": {
                        "enableAudioMetadata": False,
                        "enableAudioPts": False,
                        "enablePublishedUserList": True,
                        "maxSubscription": 50,
                        "enableUserLicenseCheck": True,
                        "enableRTX": True,
                        "enableInstantVideo": self._prefer_instant_video,
                        "enableDataStream2": False,
                        "enableAutFeedback": True,
                        "enableUserAutoRebalanceCheck": True,
                        "enableXR": True,
                        "enableLossbasedBwe": True,
                        "enableAutCC": True,
                        "enablePreallocPC": False,
                        "enablePubTWCC": False,
                        "enableSubTWCC": True,
                        "enablePubRTX": True,
                        "enableSubRTX": True,
                    }
                },
                "join_ts": int(time.time() * 1000),
                "ortc": ortc_info,
            },
        }

    def _convert_candidates_to_ortc(self) -> list[dict[str, Any]]:
        """Convert browser ICE candidates to Agora ORTC format."""
        converted: list[dict[str, Any]] = []

        for candidate in self.candidates:
            candidate_string = candidate.candidate
            if not candidate_string:
                continue

            candidate_string = candidate_string.removeprefix("candidate:")

            parts = candidate_string.split()
            if len(parts) < 8:
                continue

            try:
                converted.append(
                    {
                        "foundation": parts[0],
                        "ip": parts[4],
                        "port": int(parts[5]),
                        "priority": int(parts[3]),
                        "protocol": parts[2],
                        "type": parts[7],
                    }
                )
            except (TypeError, ValueError):
                continue

        return converted

    @staticmethod
    def _parse_offer_sdp(offer_sdp: str) -> OfferSdpInfo | None:
        """Parse browser SDP offer using sdp_transform."""
        try:
            parsed_sdp = sdp_parse(offer_sdp)

            fingerprint = ""
            if "fingerprint" in parsed_sdp:
                fingerprint = parsed_sdp["fingerprint"].get("hash", "")
            else:
                for media in parsed_sdp.get("media", []):
                    if "fingerprint" in media:
                        fingerprint = media["fingerprint"].get("hash", "")
                        break

            ice_ufrag = parsed_sdp.get("iceUfrag", "")
            ice_pwd = parsed_sdp.get("icePwd", "")
            if not ice_ufrag or not ice_pwd:
                for media in parsed_sdp.get("media", []):
                    if not ice_ufrag and "iceUfrag" in media:
                        ice_ufrag = media["iceUfrag"]
                    if not ice_pwd and "icePwd" in media:
                        ice_pwd = media["icePwd"]
                    if ice_ufrag and ice_pwd:
                        break

            audio_extensions: list[dict[str, Any]] = []
            video_extensions: list[dict[str, Any]] = []
            audio_direction = "sendrecv"
            video_direction = "sendrecv"

            for media in parsed_sdp.get("media", []):
                media_type = media.get("type")
                direction = media.get("direction", "sendrecv")

                if media_type == "audio":
                    audio_direction = direction
                elif media_type == "video":
                    video_direction = direction

                for extension in media.get("ext", []):
                    entry = {
                        "entry": extension.get("value"),
                        "extensionName": extension.get("uri"),
                    }
                    if media_type == "audio":
                        audio_extensions.append(entry)
                    elif media_type == "video":
                        video_extensions.append(entry)

            extmap_allow_mixed = bool(parsed_sdp.get("extmapAllowMixed", False))

            setup_role = "actpass"
            for media in parsed_sdp.get("media", []):
                if "setup" in media:
                    setup_role = media["setup"]
                    break

            return OfferSdpInfo(
                parsed_sdp=parsed_sdp,
                fingerprint=fingerprint,
                ice_ufrag=ice_ufrag,
                ice_pwd=ice_pwd,
                audio_extensions=audio_extensions,
                video_extensions=video_extensions,
                audio_direction=audio_direction,
                video_direction=video_direction,
                extmap_allow_mixed=extmap_allow_mixed,
                setup_role=setup_role,
            )
        except (TypeError, ValueError, KeyError) as err:
            LOGGER.error("Failed to parse offer SDP: %s", err)
            return None

    def _generate_answer_sdp(
        self,
        ortc: dict[str, Any],
        offer_info: OfferSdpInfo,
    ) -> str | None:
        """Generate answer SDP from Agora ORTC response."""
        try:
            ice_parameters = ortc.get("iceParameters", {})
            dtls_parameters = ortc.get("dtlsParameters", {})

            rtp_capabilities = ortc.get("rtpCapabilities", {})
            caps = (
                rtp_capabilities.get("sendrecv")
                or rtp_capabilities.get("recv")
                or rtp_capabilities.get("send")
                or rtp_capabilities
            )

            candidates = ice_parameters.get("candidates", []) or []
            ice_ufrag = ice_parameters.get("iceUfrag") or secrets.token_hex(4)
            ice_pwd = ice_parameters.get("icePwd") or secrets.token_hex(16)

            fingerprints = dtls_parameters.get("fingerprints", []) or []
            fingerprint = ""
            if fingerprints:
                primary = fingerprints[0]
                algorithm = (
                    primary.get("hashFunction") or primary.get("algorithm") or "sha-256"
                )
                fingerprint_value = primary.get("fingerprint", "")
                if fingerprint_value:
                    fingerprint = f"{algorithm} {fingerprint_value}"
            if not fingerprint:
                LOGGER.error("Missing DTLS fingerprint in Agora ORTC response")
                return None

            candidates_by_mid: dict[str, list[str]] = defaultdict(list)
            for index, candidate in enumerate(candidates):
                foundation = candidate.get("foundation", f"candidate{index}")
                protocol = candidate.get("protocol", "udp")
                priority = candidate.get("priority", 2103266323)
                ip = candidate.get("ip", "")
                port = candidate.get("port", 0)
                candidate_type = candidate.get("type", "host")

                line = (
                    "a=candidate:"
                    f"{foundation} 1 {protocol} {priority} {ip} {port} typ {candidate_type}"
                )
                if candidate.get("generation") is not None:
                    line += f" generation {candidate.get('generation')}"
                candidates_by_mid["*"].append(line)

            audio_codecs = caps.get("audioCodecs", []) or []
            video_codecs = caps.get("videoCodecs", []) or []
            audio_extensions = caps.get("audioExtensions", []) or []
            video_extensions = caps.get("videoExtensions", []) or []

            def _answer_direction(offer_direction: str) -> str:
                if offer_direction == "sendonly":
                    return "recvonly"
                if offer_direction == "recvonly":
                    return "sendonly"
                if offer_direction == "sendrecv":
                    return "sendrecv"
                return "inactive"

            media_sections = offer_info.parsed_sdp.get("media", []) or []
            if not media_sections:
                return None

            bundle_group = (
                offer_info.parsed_sdp.get("groups", [{}])[0]
                if offer_info.parsed_sdp.get("groups")
                else {}
            )
            bundle_mids = bundle_group.get("mids", "0 1")

            sdp_lines = [
                "v=0",
                "o=- 0 0 IN IP4 127.0.0.1",
                "s=AgoraGateway",
                "t=0 0",
                f"a=group:BUNDLE {bundle_mids}",
                "a=ice-lite",
            ]

            if offer_info.extmap_allow_mixed:
                sdp_lines.append("a=extmap-allow-mixed")
            sdp_lines.append("a=msid-semantic: WMS")

            for index, media in enumerate(media_sections):
                media_type = media.get("type", "audio")
                offer_direction = media.get("direction", "sendonly")
                answer_direction = _answer_direction(offer_direction)
                mid = str(media.get("mid", str(index)))

                codecs = audio_codecs if media_type == "audio" else video_codecs
                extensions = (
                    audio_extensions if media_type == "audio" else video_extensions
                )

                payload_types = [str(codec.get("payloadType")) for codec in codecs]
                if not payload_types:
                    payload_types = str(media.get("payloads", "")).split()

                payloads = " ".join(payload_types)

                sdp_lines.extend(
                    [
                        f"m={media_type} 9 UDP/TLS/RTP/SAVPF {payloads}",
                        "c=IN IP4 127.0.0.1",
                        "a=rtcp:9 IN IP4 0.0.0.0",
                        f"a=ice-ufrag:{ice_ufrag}",
                        f"a=ice-pwd:{ice_pwd}",
                        "a=ice-options:trickle",
                        f"a=fingerprint:{fingerprint}",
                        "a=setup:active",
                        f"a=mid:{mid}",
                    ]
                )

                sdp_lines.extend(candidates_by_mid.get("*", []))

                offer_extensions = (
                    offer_info.audio_extensions
                    if media_type == "audio"
                    else offer_info.video_extensions
                )
                offer_ext_map = {
                    extension.get("extensionName"): extension.get("entry")
                    for extension in offer_extensions
                }

                for extension in extensions:
                    extension_name = extension.get("extensionName")
                    if extension_name in offer_ext_map:
                        sdp_lines.append(
                            f"a=extmap:{offer_ext_map[extension_name]} {extension_name}"
                        )

                sdp_lines.extend(
                    [f"a={answer_direction}", "a=rtcp-mux", "a=rtcp-rsize"]
                )

                for codec in codecs:
                    payload_type = codec.get("payloadType")
                    rtp_map = codec.get("rtpMap", {})
                    codec_name = rtp_map.get("encodingName", "")
                    clock_rate = rtp_map.get("clockRate", 90000)
                    encoding_parameters = rtp_map.get("encodingParameters")

                    if encoding_parameters:
                        sdp_lines.append(
                            "a=rtpmap:"
                            f"{payload_type} {codec_name}/{clock_rate}/{encoding_parameters}"
                        )
                    else:
                        sdp_lines.append(
                            f"a=rtpmap:{payload_type} {codec_name}/{clock_rate}"
                        )

                    for feedback in codec.get("rtcpFeedbacks", []):
                        feedback_type = feedback.get("type")
                        feedback_parameter = feedback.get("parameter")
                        if feedback_parameter:
                            sdp_lines.append(
                                "a=rtcp-fb:"
                                f"{payload_type} {feedback_type} {feedback_parameter}"
                            )
                        else:
                            sdp_lines.append(
                                f"a=rtcp-fb:{payload_type} {feedback_type}"
                            )

                    fmtp = codec.get("fmtp", {})
                    parameters = fmtp.get("parameters", {}) if fmtp else {}
                    if parameters:
                        parameter_string = ";".join(
                            f"{key}={value}" for key, value in parameters.items()
                        )
                        sdp_lines.append(f"a=fmtp:{payload_type} {parameter_string}")

            answer_sdp = "\r\n".join(sdp_lines) + "\r\n"
            return answer_sdp if self._validate_sdp(answer_sdp) else None
        except (AttributeError, TypeError, ValueError) as err:
            LOGGER.error("Failed to generate answer SDP: %s", err)
            return None

    @staticmethod
    def _validate_sdp(sdp: str) -> bool:
        """Validate mandatory SDP lines."""
        if not sdp.strip():
            return False

        has_version = False
        has_origin = False
        has_session_name = False
        has_timing = False
        media_count = 0

        for line in sdp.split("\r\n"):
            if line.startswith("v="):
                has_version = True
            elif line.startswith("o="):
                has_origin = True
            elif line.startswith("s="):
                has_session_name = True
            elif line.startswith("t="):
                has_timing = True
            elif line.startswith("m="):
                media_count += 1

        return (
            has_version
            and has_origin
            and has_session_name
            and has_timing
            and media_count >= 1
        )

    @property
    def is_connected(self) -> bool:
        """Return websocket connectivity state."""
        return self._connection_state == "CONNECTED"

    async def disconnect(self) -> None:
        """Close websocket and cancel background tasks."""
        tasks_to_wait: list[asyncio.Task[None]] = []
        current_task = asyncio.current_task()

        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
            if self._ping_task is not current_task:
                tasks_to_wait.append(self._ping_task)
        self._ping_task = None

        if self._subscribe_retry_task and not self._subscribe_retry_task.done():
            self._subscribe_retry_task.cancel()
            if self._subscribe_retry_task is not current_task:
                tasks_to_wait.append(self._subscribe_retry_task)
        self._subscribe_retry_task = None

        if self._message_loop_task and not self._message_loop_task.done():
            self._message_loop_task.cancel()
            if self._message_loop_task is not current_task:
                tasks_to_wait.append(self._message_loop_task)
        self._message_loop_task = None

        if tasks_to_wait:
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)

        if self._websocket:
            with contextlib.suppress(WebSocketException):
                await self._websocket.close()
            self._websocket = None

        self._joined = False
        self._connection_state = "DISCONNECTED"
        self._video_streams.clear()
        self._subscribed_video_streams.clear()
