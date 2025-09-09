import asyncio
import json
import logging
import secrets
import ssl
import time
from dataclasses import dataclass
from typing import Any, Optional
import websockets
from homeassistant.core import HomeAssistant
from sdp_transform import parse as sdp_parse

_LOGGER = logging.getLogger(__name__)

def _create_ws_ssl_context() -> ssl.SSLContext:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context

_SSL_CONTEXT = _create_ws_ssl_context()

@dataclass
class AddressEntry:
    """Data container for Agora's edge server"""
    ip: str
    port: int
    ticket: str

@dataclass
class ResponseInfo:
    """Data container for Agore API response"""
    code: int
    addresses: list[AddressEntry]
    server_ts: int
    uid: int
    cid: int
    cname: str
    detail: dict[str, str]
    flag: int
    opid: int
    cert: str

@dataclass
class SdpInfo:
    """SDP data for WebRTC"""
    parsed_sdp: dict
    fingerprint: str
    ice_ufrag: str
    ice_pwd: str
    audio_codecs: list[dict]
    video_codecs: list[dict]
    audio_extensions: list[dict]
    video_extensions: list[dict]

class AgoraWebSocketHandler:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._websocket: Optional[websockets.client.ClientConnection] = None
        self._connection_state = "DISCONNECTED"
        self._message_handlers = {
            "answer": self._handle_answer,
            "error": self._handle_error,
        }
        self._response_handlers = {}

    async def connect_and_join(self, agora_data: Any, offer_sdp: str, session_id: str) -> Optional[str]:
        _LOGGER.debug("Starting Agora WebSocket connection for session %s", session_id)
        edge_info = await self._get_agora_edge_services(agora_data)
        if not edge_info:
            _LOGGER.error("Failed to get Agora edge services")
            return None

        sdp_info = self._parse_offer_sdp(offer_sdp)
        if not sdp_info:
            _LOGGER.error("Failed to parse offer SDP")
            return None

        edge_address = edge_info.addresses[0]
        edge_ip_dashed = edge_address.ip.replace(".", "-")
        ws_url = f"wss://{edge_ip_dashed}.edge.agora.io:{edge_address.port}"

        try:
            async with websockets.connect(ws_url, ssl=_SSL_CONTEXT, ping_timeout=30, close_timeout=30) as websocket:
                self._websocket = websocket
                self._connection_state = "CONNECTED"
                _LOGGER.debug("Connected to Agora WebSocket: %s", ws_url)

                join_message = self._create_join_message(agora_data, offer_sdp, edge_info, sdp_info)
                await websocket.send(json.dumps(join_message))
                _LOGGER.debug("Sent join message to Agora %s", join_message)

                return await self._handle_websocket_messages(websocket, session_id, sdp_info)
        except (websockets.exceptions.WebSocketException, json.JSONDecodeError) as ex:
            _LOGGER.error("WebSocket connection failed: %s", ex)
            self._connection_state = "DISCONNECTED"
            return None

    async def _handle_websocket_messages(self, websocket: websockets.WebSocketClientProtocol, session_id: str, sdp_info: SdpInfo) -> Optional[str]:
        try:
            async for message in websocket:
                response = json.loads(message)
                _LOGGER.debug("Received Agora message: %s", response)
                message_type = response.get("_type")
                message_id = response.get("_id")

                if message_id and message_id in self._response_handlers:
                    future = self._response_handlers.pop(message_id)
                    if not future.done():
                        future.set_result(response)
                    continue

                if message_type in self._message_handlers:
                    result = await self._message_handlers[message_type](response)
                    if result:
                        return result

                if response.get("_result") == "success":
                    return await self._handle_join_success(response, sdp_info)

        except websockets.exceptions.WebSocketException as ex:
            _LOGGER.error("WebSocket communication error: %s", ex)
            self._connection_state = "DISCONNECTED"

        _LOGGER.warning("No proper WebSocket response received, generating fallback SDP")
        return self._generate_fallback_sdp()

    async def _handle_join_success(self, response: dict, sdp_info: SdpInfo) -> Optional[str]:
        message = response.get("_message", {})
        ortc = message.get("ortc", {})
        if not ortc:
            _LOGGER.error("No ORTC parameters in join success response")
            _LOGGER.debug("Full response message: %s", message)
            return None

        answer_sdp = self._generate_answer_sdp(ortc, sdp_info)
        if answer_sdp:
            _LOGGER.info("Generated answer SDP from Agora ORTC parameters")
            _LOGGER.debug("Generated SDP: %s", answer_sdp)
            return answer_sdp

        _LOGGER.error("Failed to generate answer SDP")
        return None

    async def _handle_answer(self, response: dict) -> Optional[str]:
        message = response.get("_message", {})
        sdp = message.get("sdp")
        if sdp:
            _LOGGER.info("Received direct answer SDP from Agora")
            return sdp
        return None

    async def _handle_error(self, response: dict) -> None:
        message = response.get("_message", {})
        error = message.get("error", "Unknown error")
        _LOGGER.error("Agora WebSocket error: %s", error)

    async def _get_agora_edge_services(self, agora_data: Any) -> Optional[ResponseInfo]:
        # Placeholder to fetch Agora edge info from Petkit stream data
        try:
            address = AddressEntry(ip="1.2.3.4", port=443, ticket="dummy-ticket")
            response_info = ResponseInfo(
                code=0,
                addresses=[address],
                server_ts=int(time.time()),
                uid=int(agora_data.uid),
                cid=0,
                cname=agora_data.channelName,
                detail={},
                flag=0,
                opid=0,
                cert="",
            )
            return response_info
        except Exception as ex:
            _LOGGER.error("Failed to get Agora edge services: %s", ex)
            return None

    def _parse_offer_sdp(self, offer_sdp: str) -> Optional[SdpInfo]:
        try:
            parsed_sdp = sdp_parse(offer_sdp)
            fingerprint = ""
            if "fingerprint" in parsed_sdp:
                fingerprint = parsed_sdp["fingerprint"]["hash"]
            else:
                for media in parsed_sdp.get("media", []):
                    if "fingerprint" in media:
                        fingerprint = media["fingerprint"]["hash"]
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

            audio_codecs = []
            video_codecs = []
            audio_extensions = []
            video_extensions = []

            return SdpInfo(
                parsed_sdp=parsed_sdp,
                fingerprint=fingerprint,
                ice_ufrag=ice_ufrag,
                ice_pwd=ice_pwd,
                audio_codecs=audio_codecs,
                video_codecs=video_codecs,
                audio_extensions=audio_extensions,
                video_extensions=video_extensions,
            )
        except Exception as ex:
            _LOGGER.error("Failed to parse offer SDP: %s", ex)
            return None

    def _create_join_message(self, agora_data: Any, offer_sdp: str, edge_info: ResponseInfo, sdp_info: SdpInfo) -> dict:
        message_id = secrets.token_hex(3)
        process_id = f"process-{secrets.token_hex(4)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(6)}"

        return {
            "_id": message_id,
            "_type": "join_v3",
            "_message": {
                "p2p_id": 1,
                "session_id": secrets.token_hex(16).upper(),
                "app_id": agora_data.appid,
                "channel_key": agora_data.token,
                "channel_name": agora_data.channelName,
                "sdk_version": "4.23.4",
                "browser": "Mozilla/5.0",
                "process_id": process_id,
                "mode": "live",
                "codec": "vp8",
                "role": "audience",
                "has_changed_gateway": False,
                "ap_response": {
                    "code": edge_info.code,
                    "server_ts": edge_info.server_ts,
                    "uid": int(agora_data.uid),
                    "cid": edge_info.cid,
                    "cname": agora_data.channelName,
                    "detail": edge_info.detail,
                    "flag": edge_info.flag,
                    "opid": edge_info.opid,
                    "cert": edge_info.cert,
                    "ticket": edge_info.addresses[0].ticket,
                },
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
                        "enableDataStream2": False,
                        "enableUserAutoRebalanceCheck": True,
                        "enableXR": True,
                        "enableLossbasedBwe": True,
                        "enablePreallocPC": False,
                        "enablePubTWCC": False,
                        "enableSubTWCC": True,
                        "enablePubRTX": True,
                        "enableSubRTX": True,
                    },
                    "join_ts": int(time.time() * 1000),
                    "ortc": {
                        "iceParameters": {
                            "iceUfrag": sdp_info.ice_ufrag,
                            "icePwd": sdp_info.ice_pwd,
                        },
                        "dtlsParameters": {
                            "fingerprints": [
                                {
                                    "hashFunction": "sha-256",
                                    "fingerprint": sdp_info.fingerprint,
                                }
                            ],
                            "version": "2",
                        },
                        "rtpCapabilities": {
                            "send": {
                                "audioCodecs": sdp_info.audio_codecs,
                                "audioExtensions": sdp_info.audio_extensions,
                                "videoCodecs": sdp_info.video_codecs,
                                "videoExtensions": sdp_info.video_extensions,
                            },
                            "recv": {
                                "audioCodecs": [],
                                "audioExtensions": [],
                                "videoCodecs": [],
                                "videoExtensions": [],
                            },
                            "sendrecv": {
                                "audioCodecs": sdp_info.audio_codecs,
                                "audioExtensions": sdp_info.audio_extensions,
                                "videoCodecs": sdp_info.video_codecs,
                                "videoExtensions": sdp_info.video_extensions,
                            },
                        },
                    },
                },
            },
        }

    def _generate_answer_sdp(self, ortc: dict, sdp_info: SdpInfo) -> Optional[str]:
        # TODO: Implémenter la génération d'un SDP en réponse à partir des paramètres ORTC reçus et info SDP
        return "v=0\n...SDP généré..."

    def _generate_fallback_sdp(self) -> str:
        # Fournir un SDP fallback en cas d'échec de négociation
        return "v=0\no=- 0 0 IN IP4 127.0.0.1\ns=-\nt=0 0\n...fallback SDP..."

    async def disconnect(self) -> None:
        if self._websocket:
            await self._websocket.close()
            self._websocket = None
            self._connection_state = "DISCONNECTED"

    @property
    def is_connected(self) -> bool:
        return self._connection_state == "CONNECTED"
