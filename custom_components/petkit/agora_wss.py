"""Agora WebSocket handler for WebRTC streaming."""

from dataclasses import dataclass
import json
import logging
import secrets
import ssl
import time
from typing import Any, Optional

import aiohttp
from pypetkitapi import LiveFeed
from sdp_transform import parse as sdp_parse
import websockets

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _create_ws_ssl_context() -> ssl.SSLContext:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context


_SSL_CONTEXT = _create_ws_ssl_context()


@dataclass
class AddressEntry:
    """Data container for Agora edge server address information.

    Attributes:
        ip (str): The IP address of the edge server.
        port (int): The port number to connect to.
        ticket (str): Ticket string for authentication.

    """

    ip: str
    port: int
    ticket: str


@dataclass
class ResponseInfo:
    """Data container for Agora API response information.

    Attributes:
        code (int): Status code of the response.
        addresses (list[AddressEntry]): List of edge server address entries.
        server_ts (int): Server timestamp.
        uid (int): User ID.
        cid (int): Connection ID.
        cname (str): Channel name.
        detail (dict[str, str]): Additional detail information.
        flag (int): Flag parameter.
        opid (int): Operation ID.
        cert (str): Certificate string.

    """

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
    """Data container for parsed SDP (Session Description Protocol) information.

    Attributes:
        parsed_sdp (dict): Parsed SDP as a dict.
        fingerprint (str): DTLS fingerprint extracted from SDP.
        ice_ufrag (str): ICE username fragment.
        ice_pwd (str): ICE password.
        audio_codecs (list[dict]): List of audio codec descriptions.
        video_codecs (list[dict]): List of video codec descriptions.
        audio_extensions (list[dict]): List of audio extensions.
        video_extensions (list[dict]): List of video extensions.

    """

    parsed_sdp: dict
    fingerprint: str
    ice_ufrag: str
    ice_pwd: str
    audio_codecs: list[dict]
    video_codecs: list[dict]
    audio_extensions: list[dict]
    video_extensions: list[dict]


class AgoraWebSocketHandler:
    """Handler for Agora WebSocket communications and WebRTC negotiation.

    This class manages connection to Agora edge servers, sending join messages,
    handling SDP offer/answer exchange, and fallback SDP generation.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the AgoraWebSocketHandler."""
        self.hass = hass
        self._websocket: Optional[websockets.client.ClientConnection] = None
        self._connection_state = "DISCONNECTED"
        self._message_handlers = {
            "answer": self._handle_answer,
            "error": self._handle_error,
        }
        self._response_handlers = {}

    async def connect_and_join(
        self, agora_data: LiveFeed, offer_sdp: str, session_id: str
    ) -> Optional[str]:
        """Establish WebSocket connection to Agora edge server and join the session.

        Args:
            agora_data (LiveFeed): Agora stream subscription data containing app ID, channel, and token.
            offer_sdp (str): The SDP offer string from the WebRTC client.
            session_id (str): Unique session identifier.

        Returns:
            Optional[str]: The SDP answer if negotiation succeeds, None otherwise.

        """
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
            async with websockets.connect(
                ws_url, ssl=_SSL_CONTEXT, ping_timeout=10, close_timeout=10
            ) as websocket:
                self._websocket = websocket
                self._connection_state = "CONNECTED"
                _LOGGER.debug("Connected to Agora WebSocket: %s", ws_url)

                join_message = self._create_join_message(
                    agora_data, offer_sdp, edge_info, sdp_info
                )
                await websocket.send(json.dumps(join_message))
                _LOGGER.debug("Sent join message to Agora %s", join_message)

                return await self._handle_websocket_messages(
                    websocket, session_id, sdp_info
                )
        except (websockets.exceptions.WebSocketException, json.JSONDecodeError) as ex:
            _LOGGER.error("WebSocket connection failed: %s", ex)
            self._connection_state = "DISCONNECTED"
            return None

    async def _handle_websocket_messages(
        self,
        websocket: websockets.WebSocketClientProtocol,
        session_id: str,
        sdp_info: SdpInfo,
    ) -> Optional[str]:
        """Receive and process messages from the Agora WebSocket.

        Args:
            websocket (websockets.WebSocketClientProtocol): The connected WebSocket client.
            session_id (str): Session ID for logging and context.
            sdp_info (SdpInfo): Parsed SDP offer information.

        Returns:
            Optional[str]: SDP answer string if negotiation completes, else None.

        """
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

        _LOGGER.warning(
            "No proper WebSocket response received, generating fallback SDP"
        )
        return self._generate_fallback_sdp()

    async def _handle_join_success(
        self, response: dict, sdp_info: SdpInfo
    ) -> Optional[str]:
        """Handle a successful join response from Agora.

        Args:
            response (dict): JSON response from Agora containing ORTC parameters.
            sdp_info (SdpInfo): Parsed SDP offer information.

        Returns:
            Optional[str]: Generated SDP answer string or None if failure occurs.

        """
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
        """Handle an answer message from Agora.

        Args:
            response (dict): JSON answer message containing SDP.

        Returns:
            Optional[str]: SDP answer if present, else None.

        """
        message = response.get("_message", {})
        sdp = message.get("sdp")
        if sdp:
            _LOGGER.info("Received direct answer SDP from Agora")
            return sdp
        return None

    async def _handle_error(self, response: dict) -> None:
        """Handle error message received from Agora WebSocket.

        Args:
            response (dict): JSON error message.

        Logs the error detail.

        """
        message = response.get("_message", {})
        error = message.get("error", "Unknown error")
        _LOGGER.error("Agora WebSocket error: %s", error)

    async def _get_agora_edge_services(
        self, agora_data: LiveFeed
    ) -> ResponseInfo | None:
        """Retrieve Agora edge services information from the API.

        Args:
            agora_data (LiveFeed): Agora stream subscription data.

        Returns:
            ResponseInfo | None: Parsed response info if successful, else None.

        """
        app_id = agora_data.app_rtm_user_id
        channel_name = agora_data.channel_id
        token = agora_data.rtc_token
        uid = 0  # Not working with agora_data.dev_rtm_user_id

        # Generate required IDs for the API call
        client_ts = int(time.time() * 1000)
        opid = secrets.randbelow(2**31)
        sid = secrets.token_hex(16).upper()

        # Create the request payload
        request_payload = {
            "appid": app_id,
            "client_ts": client_ts,
            "opid": opid,
            "sid": sid,
            "request_bodies": [
                {
                    "uri": 22,
                    "buffer": {
                        "cname": channel_name,
                        "detail": {"11": "CN,GLOBAL", "17": "1", "22": "CN,GLOBAL"},
                        "key": token,
                        "service_ids": [11, 26],
                        "uid": uid,
                    },
                }
            ],
        }

        # Create multipart form data using aiohttp.MultipartWriter
        writer = aiohttp.MultipartWriter("form-data")
        part = writer.append(json.dumps(request_payload))
        part.set_content_disposition("form-data", name="request")

        headers = {
            "User-Agent": "Home Assistant WebRTC",
        }

        api_url = "https://webrtc2-ap-web-1.agora.io/api/v2/transpond/webrtc?v=2"

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    api_url,
                    data=writer,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response,
            ):
                if response.status != 200:
                    _LOGGER.error("Agora API returned status %s", response.status)
                    raise aiohttp.ClientError(f"API returned status {response.status}")

                # Read response as JSON
                response_text = await response.text()
                _LOGGER.debug("Agora API raw response: %s", response_text)

                response_data = json.loads(response_text)
                _LOGGER.debug("Agora API parsed response: %s", response_data)

                # Extract edge services from response
                response_bodies = response_data.get("response_body", [])
                for body in reversed(response_bodies):
                    buffer = body.get("buffer", {})
                    if buffer and buffer.get("flag") == 4096:
                        edges_services = buffer.get("edges_services", [])
                        if edges_services:
                            return ResponseInfo(
                                code=buffer["code"],
                                addresses=[
                                    AddressEntry(
                                        ip=es["ip"],
                                        port=es["port"],
                                        ticket=buffer["cert"],
                                    )
                                    for es in edges_services
                                ],
                                server_ts=response_data["enter_ts"],
                                uid=buffer["uid"],
                                cid=buffer["cid"],
                                cname=buffer["cname"],
                                detail={
                                    **buffer.get("detail", {}),
                                    **response_data.get("detail", {}),
                                },
                                flag=buffer["flag"],
                                opid=response_data["opid"],
                                cert=buffer["cert"],
                            )

                # Fallback if no edge services found
                _LOGGER.warning(
                    "No edge services found in Agora API response, using fallback"
                )
                raise aiohttp.ClientError("No edge services available")

        except (aiohttp.ClientError, json.JSONDecodeError) as ex:
            _LOGGER.error("Failed to get Agora edge services: %s", ex)
            return None

    def _parse_offer_sdp(self, offer_sdp: str) -> Optional[SdpInfo]:
        """Parse the SDP offer string and extract WebRTC parameters.

        Args:
            offer_sdp (str): The SDP offer string received from the client.

        Returns:
            Optional[SdpInfo]: Parsed SDP information or None if parsing fails.

        """
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

    def _create_join_message(
        self,
        agora_data: Any,
        offer_sdp: str,
        edge_info: ResponseInfo,
        sdp_info: SdpInfo,
    ) -> dict:
        """Create the join message to send to Agora WebSocket.

        Args:
            agora_data (Any): Agora stream subscription data.
            offer_sdp (str): SDP offer string.
            edge_info (ResponseInfo): Edge server information from Agora API.
            sdp_info (SdpInfo): Parsed SDP offer information.

        Returns:
            dict: Join message dictionary ready to be JSON serialized.

        """
        message_id = secrets.token_hex(3)
        process_id = f"process-{secrets.token_hex(4)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(6)}"

        return {
            "_id": message_id,
            "_type": "join_v3",
            "_message": {
                "p2p_id": 1,
                "session_id": secrets.token_hex(16).upper(),
                "app_id": agora_data.app_rtm_user_id,
                "channel_key": agora_data.rtc_token,
                "channel_name": agora_data.channel_id,
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
                    "uid": 0,  # Not working with agora_data.dev_rtm_user_id,
                    "cid": edge_info.cid,
                    "cname": agora_data.channel_id,
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

    def _generate_answer_sdp(
        self, ortc: dict[str, Any], sdp_info: SdpInfo
    ) -> str | None:
        """Generate SDP answer string from ORTC parameters and SDP offer info.

        Args:
            ortc (dict[str, Any]): ORTC parameters from Agora join success.
            sdp_info (SdpInfo): Parsed SDP offer information.

        Returns:
            Optional[str]: Generated SDP answer string or None if generation fails.

        """
        try:
            ice_params = ortc.get("iceParameters", {})
            dtls_params = ortc.get("dtlsParameters", {})
            rtp_caps = ortc.get("rtpCapabilities", {}).get("sendrecv", {})

            _LOGGER.debug("ICE params: %s", ice_params)
            _LOGGER.debug("DTLS params: %s", dtls_params)
            _LOGGER.debug("RTP caps: %s", rtp_caps)

            # Extract ICE candidates
            candidates = ice_params.get("candidates", [])
            ice_ufrag = ice_params.get("iceUfrag", "")
            ice_pwd = ice_params.get("icePwd", "")

            # Use fallback values if ICE parameters are missing
            if not ice_ufrag:
                ice_ufrag = secrets.token_hex(4)
                _LOGGER.warning("Using fallback ICE ufrag: %s", ice_ufrag)
            if not ice_pwd:
                ice_pwd = secrets.token_hex(16)
                _LOGGER.warning("Using fallback ICE pwd")

            # Extract DTLS fingerprint
            fingerprints = dtls_params.get("fingerprints", [])
            fingerprint = ""
            if fingerprints:
                fp = fingerprints[0]
                fingerprint = (
                    f"{fp.get('algorithm', 'sha-256')} {fp.get('fingerprint', '')}"
                )

            # Use fallback fingerprint if missing
            if not fingerprint:
                fallback_fingerprint = ":".join(
                    [secrets.token_hex(1).upper() for _ in range(32)]
                )
                fingerprint = f"sha-256 {fallback_fingerprint}"
                _LOGGER.warning("Using fallback fingerprint")

            # Build candidate lines
            candidate_lines = []
            for i, candidate in enumerate(candidates):
                candidate_line = (
                    f"a=candidate:{candidate.get('foundation', f'candidate{i}')} "
                    f"1 {candidate.get('protocol', 'udp')} "
                    f"{candidate.get('priority', 2103266323)} "
                    f"{candidate.get('ip', '')} "
                    f"{candidate.get('port', 4701)} "
                    f"typ {candidate.get('type', 'host')}"
                )
                candidate_lines.append(candidate_line)

            # Build codec lines from RTP capabilities
            video_codecs = rtp_caps.get("videoCodecs", [])
            audio_codecs = rtp_caps.get("audioCodecs", [])

            _LOGGER.debug("Video codecs: %s", video_codecs)
            _LOGGER.debug("Audio codecs: %s", audio_codecs)

            # Find VP8 codec for video
            vp8_payload = None
            vp8_payload_list = []
            for codec in video_codecs:
                if codec.get("rtpMap", {}).get("encodingName", "").upper() == "VP8":
                    vp8_payload = codec.get("payloadType")
                    vp8_payload_list.append(codec.get("payloadType"))
                    break

            # Find Opus codec for audio
            opus_payload = None
            opus_payload_list = []
            for codec in audio_codecs:
                if codec.get("rtpMap", {}).get("encodingName", "").upper() == "OPUS":
                    opus_payload = codec.get("payloadType")
                    opus_payload_list.append(codec.get("payloadType"))
                    break

            # Use default payload types if not found
            if vp8_payload is None:
                vp8_payload = 120
                _LOGGER.warning(
                    "VP8 codec not found in RTP capabilities, using default payload %s",
                    vp8_payload,
                )

            if opus_payload is None:
                opus_payload = 109
                _LOGGER.warning(
                    "Opus codec not found in RTP capabilities, using default payload %s",
                    opus_payload,
                )

            # Build basic SDP answer
            sdp_lines = [
                "v=0",
                f"o=- {sdp_info.parsed_sdp['origin']['sessionId']} {sdp_info.parsed_sdp['origin']['sessionVersion']} IN IP4 127.0.0.1",
                "s=-",
                "t=0 0",
                "a=group:BUNDLE 0 1",
                "a=msid-semantic: WMS",
                # Audio m-line
                f"m=audio 9 UDP/TLS/RTP/SAVPF {' '.join([str(i) for i in opus_payload_list])}",
                "c=IN IP4 0.0.0.0",
                "a=rtcp:9 IN IP4 0.0.0.0",
                f"a=ice-ufrag:{ice_ufrag}",
                f"a=ice-pwd:{ice_pwd}",
                "a=ice-options:trickle",
                f"a=fingerprint:{fingerprint}",
                "a=setup:active",
                "a=mid:0",
                "a=sendrecv",
                "a=rtcp-mux",
                f"a=rtpmap:{opus_payload} opus/48000/2",
                # Video m-line
                f"m=video 9 UDP/TLS/RTP/SAVPF {' '.join([str(i) for i in vp8_payload_list])}",
                "c=IN IP4 0.0.0.0",
                "a=rtcp:9 IN IP4 0.0.0.0",
                f"a=ice-ufrag:{ice_ufrag}",
                f"a=ice-pwd:{ice_pwd}",
                "a=ice-options:trickle",
                f"a=fingerprint:{fingerprint}",
                "a=setup:active",
                "a=mid:1",
                "a=sendrecv",
                "a=rtcp-mux",
                f"a=rtpmap:{vp8_payload} VP8/90000",
            ]

            # Add candidates
            sdp_lines.extend(candidate_lines)

            generated_sdp = "\r\n".join(sdp_lines) + "\r\n"
            _LOGGER.debug("Generated SDP lines count: %s", len(sdp_lines))
            _LOGGER.debug("Generated SDP content: %s", generated_sdp)

            # Validate SDP format
            if self._validate_sdp(generated_sdp):
                return generated_sdp
            _LOGGER.error("Generated SDP failed validation")
            return None

        except (KeyError, ValueError, AttributeError) as ex:
            _LOGGER.error("Failed to generate answer SDP: %s", ex)
            return None

    def _validate_sdp(self, sdp: str) -> bool:
        """Validate the format and essential lines of an SDP string.

        Checks for presence of mandatory SDP lines and counts media sections.

        Args:
            sdp (str): SDP string to validate.

        Returns:
            bool: True if SDP is valid, False otherwise.

        """
        if not sdp or len(sdp.strip()) == 0:
            _LOGGER.error("SDP is empty")
            return False

        lines = sdp.split("\r\n")
        has_version = False
        has_origin = False
        has_session_name = False
        has_timing = False
        m_line_count = 0

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line.startswith("v="):
                has_version = True
            elif line.startswith("o="):
                has_origin = True
            elif line.startswith("s="):
                has_session_name = True
            elif line.startswith("t="):
                has_timing = True
            elif line.startswith("m="):
                m_line_count += 1

        if not has_version:
            _LOGGER.error("SDP missing version line (v=)")
            return False
        if not has_origin:
            _LOGGER.error("SDP missing origin line (o=)")
            return False
        if not has_session_name:
            _LOGGER.error("SDP missing session name line (s=)")
            return False
        if not has_timing:
            _LOGGER.error("SDP missing timing line (t=)")
            return False
        if m_line_count < 2:
            _LOGGER.error(
                "SDP has %s m-lines, expected 2 (audio + video)", m_line_count
            )
            return False

        _LOGGER.debug("SDP validation passed: %s m-lines found", m_line_count)
        return True

    def _generate_fallback_sdp(self) -> str:
        """Generate a basic fallback SDP answer with default parameters.

        This SDP is used if negotiation fails or no valid SDP is received.

        Returns:
            str: Fallback SDP string.

        """
        _LOGGER.info("Generating fallback SDP with default parameters")

        # Generate basic parameters
        ice_ufrag = secrets.token_hex(4)
        ice_pwd = secrets.token_hex(16)
        fallback_fingerprint = ":".join(
            [secrets.token_hex(1).upper() for _ in range(32)]
        )
        fingerprint = f"sha-256 {fallback_fingerprint}"

        # Default codec payload types
        opus_payload = 109
        vp8_payload = 120

        # Build fallback SDP answer
        sdp_lines = [
            "v=0",
            f"o=- {secrets.randbelow(2**63)} 2 IN IP4 127.0.0.1",
            "s=-",
            "t=0 0",
            "a=group:BUNDLE 0 1",
            "a=msid-semantic: WMS",
            "",
            # Audio m-line
            f"m=audio 9 UDP/TLS/RTP/SAVPF {opus_payload}",
            "c=IN IP4 0.0.0.0",
            "a=rtcp:9 IN IP4 0.0.0.0",
            f"a=ice-ufrag:{ice_ufrag}",
            f"a=ice-pwd:{ice_pwd}",
            "a=ice-options:trickle",
            f"a=fingerprint:{fingerprint}",
            "a=setup:active",
            "a=mid:0",
            "a=sendrecv",
            "a=rtcp-mux",
            f"a=rtpmap:{opus_payload} opus/48000/2",
            "",
            # Video m-line
            f"m=video 9 UDP/TLS/RTP/SAVPF {vp8_payload}",
            "c=IN IP4 0.0.0.0",
            "a=rtcp:9 IN IP4 0.0.0.0",
            f"a=ice-ufrag:{ice_ufrag}",
            f"a=ice-pwd:{ice_pwd}",
            "a=ice-options:trickle",
            f"a=fingerprint:{fingerprint}",
            "a=setup:active",
            "a=mid:1",
            "a=sendrecv",
            "a=rtcp-mux",
            f"a=rtpmap:{vp8_payload} VP8/90000",
        ]

        generated_sdp = "\r\n".join(sdp_lines) + "\r\n"
        _LOGGER.debug("Generated fallback SDP: %s", generated_sdp)

        # Validate fallback SDP
        if self._validate_sdp(generated_sdp):
            return generated_sdp
        _LOGGER.error("Fallback SDP failed validation")
        # Return a minimal valid SDP as last resort
        return self._generate_minimal_sdp()

    def _generate_minimal_sdp(self) -> str:
        """Generate a minimal valid SDP as a last resort.

        Returns:
            str: Minimal valid SDP string.

        """
        _LOGGER.warning("Generating minimal SDP as last resort")

        ice_ufrag = secrets.token_hex(4)
        ice_pwd = secrets.token_hex(16)

        return (
            "v=0\r\n"
            f"o=- {secrets.randbelow(2**63)} 2 IN IP4 127.0.0.1\r\n"
            "s=-\r\n"
            "t=0 0\r\n"
            "a=group:BUNDLE 0 1\r\n"
            "a=msid-semantic: WMS\r\n"
            "m=audio 9 UDP/TLS/RTP/SAVPF 109\r\n"
            "c=IN IP4 0.0.0.0\r\n"
            f"a=ice-ufrag:{ice_ufrag}\r\n"
            f"a=ice-pwd:{ice_pwd}\r\n"
            "a=fingerprint:sha-256 00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00\r\n"
            "a=setup:active\r\n"
            "a=mid:0\r\n"
            "a=sendrecv\r\n"
            "a=rtcp-mux\r\n"
            "a=rtpmap:109 opus/48000/2\r\n"
            "m=video 9 UDP/TLS/RTP/SAVPF 120\r\n"
            "c=IN IP4 0.0.0.0\r\n"
            f"a=ice-ufrag:{ice_ufrag}\r\n"
            f"a=ice-pwd:{ice_pwd}\r\n"
            "a=fingerprint:sha-256 00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00\r\n"
            "a=setup:active\r\n"
            "a=mid:1\r\n"
            "a=sendrecv\r\n"
            "a=rtcp-mux\r\n"
            "a=rtpmap:120 VP8/90000\r\n"
        )

    async def disconnect(self) -> None:
        """Close the WebSocket connection if it is open."""
        if self._websocket:
            await self._websocket.close()
            self._websocket = None
            self._connection_state = "DISCONNECTED"

    @property
    def is_connected(self) -> bool:
        """Check if the WebSocket connection to Agora is currently established."""
        return self._connection_state == "CONNECTED"
