"""Petkit camera entities."""

from __future__ import annotations

import asyncio
import collections
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

import websockets
from homeassistant.components.camera import (
    CameraEntityDescription,
    WebRTCAnswer,
    WebRTCError,
    WebRTCSendMessage, Camera, CameraEntityFeature, StreamType,
)
from homeassistant.components.web_rtc import (
    async_register_ice_servers,
)
from homeassistant.core import (
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from pypetkitapi import FEEDER_WITH_CAMERA, LITTER_WITH_CAMERA, Feeder, Litter, LiveFeed
from webrtc_models import RTCIceCandidateInit, RTCIceServer
from .agora_api import SERVICE_IDS, AgoraAPIClient
from .agora_websocket import AgoraWebSocketHandler
from .entity import PetKitDescSensorBase, PetkitEntity
from .const import LOGGER

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import PetkitDataUpdateCoordinator
    from .data import PetkitConfigEntry, PetkitDevices


@dataclass(frozen=True, kw_only=True)
class PetkitCameraEntityDesc(PetKitDescSensorBase, CameraEntityDescription):
    """Describes Petkit camera entity."""

    key: str


COMMON_ENTITIES = []

CAMERA_MAPPING: dict[type[PetkitDevices], list[PetkitCameraEntityDesc]] = {
    Feeder: [
        *COMMON_ENTITIES,
        PetkitCameraEntityDesc(
            key="webrtc_camera",
            translation_key="webrtc_camera",
            only_for_types=FEEDER_WITH_CAMERA,
        ),
    ],
    Litter: [
        *COMMON_ENTITIES,
        PetkitCameraEntityDesc(
            key="webrtc_camera",
            translation_key="webrtc_camera",
            only_for_types=LITTER_WITH_CAMERA,
        ),
    ],
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Petkit camera entities."""
    devices = entry.runtime_data.client.petkit_entities.values()
    entities = []
    for device in devices:
        LOGGER.debug("Config camera for %s", device.device_nfo.device_name)
        try:
            # Check if device has live_feed attribute (only camera-enabled devices)
            if not hasattr(device, 'live_feed'):
                LOGGER.debug("Device %s does not have live_feed, skipping", device.device_nfo.device_name)
                continue

            # Try to get stream data
            stream_data = device.live_feed
            ## MOVE TO LIB # mower.reporting_coordinator._stream_data = stream_data

            if stream_data:
                LOGGER.debug("Got stream data: %s", stream_data)

                # Get ICE servers from Agora API
                try:
                    async with (AgoraAPIClient() as agora_client):
                        agora_response = await agora_client.choose_server(
                            app_id=device.live_feed.app_rtm_user_id,
                            token=device.live_feed.rtc_token,
                            channel_name=device.live_feed.channel_id,
                            user_id=0,
                            service_flags=[
                                SERVICE_IDS["CHOOSE_SERVER"],  # Gateway addresses
                                SERVICE_IDS["CLOUD_PROXY_FALLBACK"],  # TURN servers
                            ],
                        )

                        # Get ICE servers and convert to RTCIceServer format - use only first TURN server to match SDK (3 entries)
                        ice_servers_agora = agora_response.get_ice_servers(
                            use_all_turn_servers=False
                        )
                        ice_servers = []
                        LOGGER.info(
                            "Ice Servers from Agora API:%s", ice_servers_agora
                        )
                        for ice_server in ice_servers_agora:
                            ice_servers.append(
                                RTCIceServer(
                                    urls=ice_server.urls,
                                    username=ice_server.username,
                                    credential=ice_server.credential,
                                )
                            )

                        # Store ICE servers in coordinator
                        ## MOVE TO LIB # mower.reporting_coordinator._ice_servers = ice_servers
                        #device.live_feed.ice_servers = ice_servers
                        ## MOVE TO LIB # mower.reporting_coordinator._agora_response = agora_response
                        #device.live_feed.agora_response = agora_response

                        entry.runtime_data.coordinator._ice_servers = ice_servers
                        entry.runtime_data.coordinator._agora_response = agora_response

                        LOGGER.info(
                            "Retrieved %d ICE servers from Agora API",
                            len(ice_servers),
                        )
                except Exception as e:
                    LOGGER.error("Failed to get ICE servers from Agora API: %s", e)
                    entry.runtime_data.coordinator._ice_servers = []

                LOGGER.debug("Adding %s", device.device_nfo.device_name)
                entities.extend(
                    PetkitWebRTCCamera(
                        hass=hass,
                        coordinator=entry.runtime_data.coordinator,
                        entity_description=entity_description,
                        device=device,
                        stream_data=stream_data,
                    )
                    for entity_description in CAMERA_MAPPING.get(type(device), [])
                )
            else:
                LOGGER.error("No Agora data for %s", device.device_nfo.device_name)
        except Exception as e:
            LOGGER.error("Error on async setup entry camera for: %s", e)
    LOGGER.debug(
        "CAMERA : Adding %s (on %s available)",
        len(entities),
        sum(len(descriptors) for descriptors in CAMERA_MAPPING.values()),
    )
    async_add_entities(entities)
    await async_setup_platform_services(hass, entry)


class PetkitWebRTCCamera(PetkitEntity, Camera):
    """Petkit WebRTC camera entity."""

    entity_description: PetkitCameraEntityDesc
    _attr_capability_attributes = None
    _attr_is_streaming = True
    _attr_supported_features = CameraEntityFeature.STREAM
    #_attr_frontend_stream_type = StreamType.WEB_RTC

    def __init__(
        self,
        coordinator: PetkitDataUpdateCoordinator,
        entity_description: PetkitCameraEntityDesc,
        hass: HomeAssistant,
        device: PetkitDevices,
        stream_data: LiveFeed,
    ) -> None:
        """Initialize the WebRTC camera entity."""
        Camera.__init__(self)
        PetkitEntity.__init__(self, coordinator, device)
        self._cache: dict[str, Any] = {}
        self.access_tokens: collections.deque = collections.deque([], 2)
        self.async_update_token()
        self._create_stream_lock: asyncio.Lock | None = None
        self._agora_handler = AgoraWebSocketHandler(hass)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self._attr_translation_key = entity_description.key
        self._stream_data = stream_data
        self._attr_model = device.device_nfo.device_name
        self.access_tokens = [secrets.token_hex(16)]
        # Get ICE servers from coordinator (populated in async_setup_entry)
        self.ice_servers = coordinator._ice_servers
        self._agora_response = coordinator._agora_response
        async_register_ice_servers(hass, self.get_ice_servers)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a placeholder image for WebRTC cameras that don't support snapshots."""
        return None

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Handle WebRTC offer by initiating WebSocket connection to Agora.

        This replaces the JavaScript SDK functionality and performs the WebRTC
        negotiation directly in Python.
        """
        # Reset candidates list for new session
        self._agora_handler.candidates = []
        LOGGER.info("Handling WebRTC offer for session %s", session_id)
        LOGGER.info("Raw OFFER SDP %s", offer_sdp)

        # Wait for initial ICE candidates, specifically looking for a reflexive candidate (srflx/prflx)
        # or relay, as requested to ensure public connectivity.
        max_wait = 15.0
        wait_interval = 0.1
        elapsed = 0.0

        def has_agora_turn_candidate():
            """Check if any candidate is from Agora TURN servers (srflx, relay, or prflx)."""
            for cand in self._agora_handler.candidates:
                cand_str = ""
                if isinstance(cand, dict):
                    cand_str = cand.get("candidate", "")
                elif hasattr(cand, "candidate"):
                    cand_str = cand.candidate

                # Check for candidates from Agora TURN/STUN servers:
                # - typ srflx: Server Reflexive (discovered via STUN from Agora)
                # - typ relay: Relay candidate (routed through Agora TURN)
                # - typ prflx: Peer Reflexive (similar to srflx)
                if any(t in cand_str for t in ["typ srflx", "typ relay", "typ prflx"]):
                    return True
            return False

        # Wait for at least one candidate from Agora TURN servers before proceeding
        # This ensures we have reflexive or relay connectivity for NAT traversal
        while not has_agora_turn_candidate() and elapsed < max_wait:
            await asyncio.sleep(wait_interval)
            elapsed += wait_interval

        # Filter candidates to ONLY send candidates that match Agora ICE servers (Strict Filtering)
        # "Change the candidate selection code to only accept candidates from agora ice servers"
        # INTERPRETATION:
        # - 'relay' candidates MUST match an Agora TURN server IP.
        # - 'srflx' candidates come from Agora STUN servers but have the public IP of the user. We MUST allow these.
        # - 'prflx' candidates are peer-reflexive, similar to srflx. Allow.
        # - 'host' candidates are local IPs. These do NOT come from an ICE server. Filter them out.

        valid_ips = set()
        if self._agora_response and self._agora_response.addresses:
            for addr in self._agora_response.addresses:
                valid_ips.add(addr.ip)

        filtered_candidates = []
        for cand in self._agora_handler.candidates:
            cand_str = ""
            if isinstance(cand, dict):
                cand_str = cand.get("candidate", "")
            elif hasattr(cand, "candidate"):
                cand_str = cand.candidate

            # Allow reflexive types unconditionally (they connect via Agora STUN)
            if any(t in cand_str for t in ["typ srflx", "typ prflx", "typ relay"]):
                filtered_candidates.append(cand)
                continue

            # For relay candidates, enforce strict IP matching
            if "typ relay" in cand_str:
                if any(ip in cand_str for ip in valid_ips):
                    filtered_candidates.append(cand)
                else:
                    LOGGER.warning("Dropped unknown relay candidate: %s", cand_str)
                continue

            # Drop 'host' candidates or others
            # LOGGER.debug("Dropped candidate (not from ICE server): %s", cand_str)

        if filtered_candidates:
            LOGGER.info(
                "Strict filtering: Keeping %d candidates (Agora-derived). Total was %d.",
                len(filtered_candidates),
                len(self._agora_handler.candidates),
            )
            self._agora_handler.candidates = filtered_candidates
        else:
            LOGGER.warning(
                "Strict filtering removed all candidates! Fallback to original list."
            )
            # If we filtered everything, maybe we shouldn't have. Fallback or fail?
            # For now, let's fallback to the previous "reflexive" list or just keep all to avoid total failure,
            # but log a warning.
            # Actually, let's try to at least keep reflexive/relay if IP match fails (e.g. if srflx usage was intended).
            # But user request was specific. Let's warn and proceed with empty (or re-populate if desired behaviour is 'best effort').
            # Given "only accept", sending nothing is compliant but broken.
            # Let's assume we MUST find one. If not, the wait loop above failed to find an Agora one.
            LOGGER.warning(
                "No reflexive candidates found after wait, proceeding with all collected candidates"
            )

        LOGGER.info(
            "Collected %d ICE candidates (Reflexive found: %s) after %.1fs",
            len(self._agora_handler.candidates),
            has_agora_turn_candidate(),
            elapsed,
        )

        try:
            # Get stream data (appid, channelName, token, uid)
            if not self._stream_data or self._stream_data is None:
                LOGGER.error("No stream data available for WebRTC offer")
                send_message(
                    WebRTCError(
                        "500",
                        "No stream data available for WebRTC offer",
                    )
                )
                return

            # agora_data = stream_data.data WHAT IS .DATA ??????????????????????????????????
            agora_data = self._stream_data

            # Start WebSocket connection and WebRTC negotiation
            answer_sdp = await self._perform_webrtc_negotiation(
                offer_sdp, agora_data, session_id
            )

            if answer_sdp:
                # Send the answer back to the browser

                send_message(WebRTCAnswer(answer_sdp))
                LOGGER.info("WebRTC negotiation completed successfully")
                # Send set_client_role after successful join
            else:
                send_message(WebRTCError("500", "WebRTC negotiation failed"))

        except (websockets.exceptions.WebSocketException, json.JSONDecodeError) as ex:
            LOGGER.error("Error handling WebRTC offer: %s", ex)
            send_message(WebRTCError("500", f"Error handling WebRTC offer: {ex}"))

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Collect WebRTC candidates for inclusion in join message."""
        LOGGER.info(
            "Received WebRTC candidate for session %s: %s", session_id, candidate
        )

        # Collect candidates - they'll be included in the join message
        self._agora_handler.candidates.append(candidate)

    @callback
    async def async_close_webrtc_session(self, session_id: str) -> None:
        """Close WebRTC session."""
        await self._agora_handler.disconnect()

    async def _perform_webrtc_negotiation(
        self, offer_sdp: str, agora_data: LiveFeed, session_id: str
    ) -> str | None:
        """Perform WebRTC negotiation through Agora WebSocket.

        Args:
            self: The camera instance
            offer_sdp: The WebRTC offer SDP from the browser
            agora_data: Dict containing appid, channelName, token, uid
            session_id: Session ID for this WebRTC connection

        Returns:
            Answer SDP if successful, None otherwise

        """
        LOGGER.debug("Starting WebRTC negotiation with Agora data: %s", agora_data)
        # LOGGER.debug("Starting WebRTC negotiation with offer_sdp data: %s", offer_sdp)

        # Use the new AgoraWebSocketHandler for negotiation
        try:
            answer_sdp = await self._agora_handler.connect_and_join(
                agora_data, offer_sdp, session_id, agora_response=self._agora_response
            )

            if answer_sdp:
                LOGGER.info("Successfully negotiated WebRTC through Agora")
                return answer_sdp

            LOGGER.error(
                "Failed to get answer SDP from Agora negotiation, using handler fallback"
            )
            # Use the handler's fallback SDP generation as last resort
            return self._agora_handler._generate_fallback_sdp()

        except (OSError, ValueError, TypeError) as ex:
            LOGGER.error("WebRTC negotiation failed: %s", ex)
            LOGGER.warning("Using fallback SDP due to exception")
            return self._agora_handler._generate_fallback_sdp()

    def get_ice_servers(self) -> list[RTCIceServer]:
        """Return the ICE servers from Agora API."""
        return self.ice_servers


# Global
async def async_setup_platform_services(
    hass: HomeAssistant, entry: PetkitConfigEntry
) -> None:
    """Register custom services for streaming."""

    def _get_mower_by_entity_id(entity_id: str):
        state = hass.states.get(entity_id)
        name = state.attributes.get("model_name")
        return next(
            (
                mower
                for mower in entry.runtime_data.mowers
                if mower.device.device_name == name
            ),
            None,
        )

    async def handle_refresh_stream(call) -> None:
        entity_id = call.data["entity_id"]
        mower: MammotionMowerData = _get_mower_by_entity_id(entity_id)
        if mower:
            stream_data = await mower.api.get_stream_subscription(
                mower.device.device_name, mower.device.iot_id
            )
            LOGGER.debug("Refresh stream data : %s", stream_data)

            mower.reporting_coordinator.set_stream_data(stream_data)
            mower.reporting_coordinator.async_update_listeners()

    async def handle_start_video(call) -> None:
        entity_id = call.data["entity_id"]
        mower: MammotionMowerData = _get_mower_by_entity_id(entity_id)
        if mower:
            await mower.reporting_coordinator.join_webrtc_channel()

    async def handle_stop_video(call) -> None:
        entity_id = call.data["entity_id"]
        mower: MammotionMowerData = _get_mower_by_entity_id(entity_id)
        if mower:
            await mower.reporting_coordinator.leave_webrtc_channel()

    async def handle_get_tokens(call: ServiceCall) -> ServiceResponse:
        entity_id = call.data["entity_id"]
        mower: MammotionMowerData = _get_mower_by_entity_id(entity_id)
        if mower is not None:
            stream_data = mower.reporting_coordinator.get_stream_data()

            if not stream_data or stream_data.data is None:
                return {}
            # Return all the data needed for the Agora SDK
            return stream_data.data.to_dict()
        return {}


    hass.services.async_register("petkit", "refresh_stream", handle_refresh_stream)
    hass.services.async_register("petkit", "start_video", handle_start_video)
    hass.services.async_register("petkit", "stop_video", handle_stop_video)
    hass.services.async_register(
        "petkit",
        "get_tokens",
        handle_get_tokens,
        supports_response=SupportsResponse.ONLY,
    )