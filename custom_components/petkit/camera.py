"""Camera platform for Petkit Smart Devices integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

from go2rtc_client.ws import (
    Go2RtcWsClient,
    WebRTCAnswer as Go2RTCAnswer,
    WebRTCCandidate as Go2RTCCandidate,
    WebRTCOffer as Go2RTCOffer,
    WsError as Go2RTCWsError,
)
from pypetkitapi import (
    FEEDER_WITH_CAMERA,
    FOUNTAIN_WITH_CAMERA,
    LITTER_WITH_CAMERA,
    Feeder,
    Litter,
    LiveFeed,
    MediaType,
    WaterFountain,
)
from webrtc_models import RTCIceCandidateInit, RTCIceServer

from homeassistant.components.camera import (
    CameraCapabilities,
    CameraEntityDescription,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.components.camera.const import StreamType
from homeassistant.components.web_rtc import async_register_ice_servers
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .agora_api import SERVICE_IDS, AgoraAPIClient, AgoraResponse
from .agora_rtm import AgoraRTMSignaling
from .const import AGORA_APP_ID, DOMAIN, LOGGER
from .coordinator import PetkitDataUpdateCoordinator
from .entity import PetkitCameraBaseEntity, PetKitDescSensorBase
from .go2rtc_stream import get_go2rtc_stream_manager
from .whep_proxy import get_whep_upstream_manager


@dataclass(frozen=True, kw_only=True)
class PetKitCameraDesc(PetKitDescSensorBase, CameraEntityDescription):
    """Description class for PetKit camera entities."""


class _BrowserSessionState(Enum):
    """Lifecycle state of a browser-to-go2rtc WebRTC session."""

    PENDING = auto()
    ACTIVE = auto()
    CLOSED = auto()
    FAILED = auto()


@dataclass
class _BrowserSession:
    """Tracks one browser WebRTC session through its lifecycle."""

    state: _BrowserSessionState
    ws_client: Go2RtcWsClient | None = None
    queued_candidates: list[str] = field(default_factory=list)


CAMERA_MAPPING: dict[type[Feeder | Litter | WaterFountain], list[PetKitCameraDesc]] = {
    Feeder: [
        PetKitCameraDesc(
            key="camera",
            translation_key="camera",
            only_for_types=FEEDER_WITH_CAMERA,
            value=lambda _device: True,
        )
    ],
    Litter: [
        PetKitCameraDesc(
            key="camera",
            translation_key="camera",
            only_for_types=LITTER_WITH_CAMERA,
            value=lambda _device: True,
        )
    ],
    WaterFountain: [
        PetKitCameraDesc(
            key="camera",
            translation_key="camera",
            only_for_types=FOUNTAIN_WITH_CAMERA,
            value=lambda _device: True,
        )
    ],
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up camera entities."""
    devices = entry.runtime_data.client.petkit_entities.values()

    entities: list[PetkitWebRTCCamera] = [
        PetkitWebRTCCamera(
            coordinator=entry.runtime_data.coordinator,
            device=device,
            entity_description=entity_description,
            hass=hass,
        )
        for device in devices
        for device_type, descriptions in CAMERA_MAPPING.items()
        if isinstance(device, device_type)
        for entity_description in descriptions
        if entity_description.is_supported(device)
    ]
    if entities:
        results = await asyncio.gather(
            *(entity.async_prepare_agora() for entity in entities),
            return_exceptions=True,
        )
        for entity, result in zip(entities, results, strict=False):
            if isinstance(result, Exception):
                LOGGER.debug(
                    "Failed to prefetch Agora context for %s: %s",
                    entity.entity_id,
                    result,
                )

    async_add_entities(entities)


class PetkitWebRTCCamera(PetkitCameraBaseEntity):
    """PetKit camera entity backed by the shared go2rtc stream."""

    entity_description: PetKitCameraDesc

    def __init__(
        self,
        coordinator: PetkitDataUpdateCoordinator,
        device: Feeder | Litter | WaterFountain,
        entity_description: PetKitCameraDesc,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the camera entity."""
        super().__init__(coordinator, device, entity_description.key)
        self.hass = hass
        self.coordinator = coordinator
        self.device = device
        self.entity_description = entity_description
        self._attr_translation_key = entity_description.translation_key

        self._agora_rtm = AgoraRTMSignaling(AGORA_APP_ID)
        self._agora_response: AgoraResponse | None = None
        self._ice_servers: list[RTCIceServer] = []
        self._remove_ice_servers: Callable[[], None] | None = None
        self._go2rtc_browser_sessions: dict[str, _BrowserSession] = {}

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self.device.id in self.coordinator.data

    @property
    def camera_capabilities(self) -> CameraCapabilities:
        """Advertise the supported frontend playback mode."""
        return CameraCapabilities(frontend_stream_types={StreamType.WEB_RTC})

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Expose the supported shared stream metadata."""
        go2rtc_manager = get_go2rtc_stream_manager(self.hass)
        attributes: dict[str, str] = {
            "whep_direct_url": self._whep_direct_url(),
        }
        if go2rtc_manager.is_available():
            attributes["go2rtc_stream_name"] = go2rtc_manager.stream_name(
                str(self.device.id)
            )
        return attributes

    async def async_added_to_hass(self) -> None:
        """Register ICE callback when entity is added."""
        await super().async_added_to_hass()
        self.hass.data.setdefault(DOMAIN, {}).setdefault("cameras", {})
        self.hass.data[DOMAIN]["cameras"][str(self.device.id)] = self
        if get_go2rtc_stream_manager(self.hass).is_available():
            try:
                await get_go2rtc_stream_manager(self.hass).async_ensure_stream(self)
            except RuntimeError as err:
                LOGGER.debug(
                    "Failed to register shared go2rtc stream for %s: %s",
                    self.device.id,
                    err,
                )
        self._remove_ice_servers = async_register_ice_servers(
            self.hass,
            self.get_ice_servers,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cleanup callbacks and websocket sessions."""
        if self._remove_ice_servers:
            self._remove_ice_servers()
            self._remove_ice_servers = None
        if DOMAIN in self.hass.data and "cameras" in self.hass.data[DOMAIN]:
            self.hass.data[DOMAIN]["cameras"].pop(str(self.device.id), None)

        if get_go2rtc_stream_manager(self.hass).is_available():
            await get_go2rtc_stream_manager(self.hass).async_remove_stream(self)
        await self._async_close_stream()
        await super().async_will_remove_from_hass()

    async def async_prepare_agora(self) -> None:
        """Best-effort prefetch for ICE servers before first offer."""
        live_feed = await self._get_live_feed()
        if live_feed is None:
            return
        await self._refresh_agora_context(live_feed)

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return bytes of camera image.

        Implementation strategy:
        1. Try to get the latest event image from device records
        2. If no event image, return default placeholder image

        Live snapshots are not extracted from the active stream. The entity falls
        back to downloaded event images, then to a static placeholder image.
        """
        LOGGER.debug(
            "async_camera_image called with width=%s, height=%s", width, height
        )

        try:
            event_image = await self._get_latest_event_image()
            if event_image:
                LOGGER.debug("Using event image for device %s", self.device.id)
                return event_image

            LOGGER.debug(
                "No image available, returning default placeholder for device %s",
                self.device.id,
            )
            return await self._get_default_image()
        except OSError as err:
            LOGGER.error("Failed to get camera image: %s", err)
        else:
            LOGGER.debug("No event image available for device %s", self.device.id)
            return None

    async def _get_latest_event_image(self) -> bytes | None:
        """Get the latest event image from device records."""
        try:
            media_coordinator = (
                self.coordinator.config_entry.runtime_data.coordinator_media
            )
            media_table = media_coordinator.media_table

            device_media = media_table.get(self.device.id, [])

            if device_media:
                image_files = [
                    media
                    for media in device_media
                    if media.media_type == MediaType.IMAGE
                ]

                if image_files:
                    latest_image = max(image_files, key=lambda m: m.timestamp)
                    LOGGER.debug(
                        "Found latest event image: %s", latest_image.full_file_path
                    )

                    import aiofiles

                    async with aiofiles.open(
                        latest_image.full_file_path, "rb"
                    ) as image_file:
                        image_data = await image_file.read()
                    LOGGER.debug(
                        "Successfully loaded event image (%d bytes)", len(image_data)
                    )
                    return image_data
        except OSError as err:
            LOGGER.debug("Failed to get event image: %s", err)
        else:
            return None

    @staticmethod
    async def _get_default_image() -> bytes | None:
        """Get the default placeholder image."""
        try:
            default_image_path = Path(__file__).parent / "img" / "play.png"

            if default_image_path.exists():
                import aiofiles

                async with aiofiles.open(default_image_path, "rb") as image_file:
                    image_data = await image_file.read()
                LOGGER.debug(
                    "Successfully loaded default camera image (%d bytes)",
                    len(image_data),
                )
                return image_data
            LOGGER.warning("Default camera image not found at: %s", default_image_path)
        except OSError as err:
            LOGGER.error("Failed to get default image: %s", err)
        else:
            return None

    async def stream_source(self) -> str | None:
        """Return the preferred source for downstream stream consumers."""
        go2rtc_manager = get_go2rtc_stream_manager(self.hass)
        if go2rtc_manager.is_available():
            rtsp_url = await go2rtc_manager.async_ensure_stream(self)
            if rtsp_url:
                return rtsp_url
        return None

    async def async_handle_async_webrtc_offer(
        self,
        offer_sdp: str,
        session_id: str,
        send_message: WebRTCSendMessage,
    ) -> None:
        """Handle browser WebRTC offer and return SDP answer."""
        await self._async_handle_go2rtc_browser_offer(
            offer_sdp,
            session_id,
            send_message,
        )

    async def async_on_webrtc_candidate(
        self,
        session_id: str,
        candidate: RTCIceCandidateInit,
    ) -> None:
        """Collect browser ICE candidates for the go2rtc session."""
        session = self._go2rtc_browser_sessions.get(session_id)
        if session is None:
            LOGGER.debug("Ignoring ICE candidate for unknown session %s", session_id)
            return
        if session.state in (_BrowserSessionState.CLOSED, _BrowserSessionState.FAILED):
            LOGGER.debug(
                "Ignoring ICE candidate for %s session %s",
                session.state.name,
                session_id,
            )
            return
        if session.state == _BrowserSessionState.PENDING:
            session.queued_candidates.append(candidate.candidate)
            return
        if session.ws_client is None:
            LOGGER.debug(
                "Session %s is ACTIVE but ws_client is None, marking FAILED",
                session_id,
            )
            session.state = _BrowserSessionState.FAILED
            self._go2rtc_browser_sessions.pop(session_id, None)
            return
        await session.ws_client.send(Go2RTCCandidate(candidate.candidate))

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Close one browser WebRTC session."""
        session = self._go2rtc_browser_sessions.pop(session_id, None)
        if session is None:
            return
        session.state = _BrowserSessionState.CLOSED
        session.queued_candidates.clear()
        if session.ws_client is not None:
            self.hass.async_create_task(session.ws_client.close())

    def get_ice_servers(self) -> list[RTCIceServer]:
        """Return cached Agora ICE servers for Home Assistant frontend."""
        return self._ice_servers

    async def _async_close_stream(self, *, send_stop: bool = False) -> None:
        """Close browser WebRTC sessions and any local RTM state."""
        sessions = list(self._go2rtc_browser_sessions.values())
        self._go2rtc_browser_sessions.clear()
        for s in sessions:
            s.state = _BrowserSessionState.CLOSED
            s.queued_candidates.clear()
        ws_clients = [s.ws_client for s in sessions if s.ws_client is not None]
        if ws_clients:
            await asyncio.gather(
                *(client.close() for client in ws_clients),
                return_exceptions=True,
            )
        try:
            await self._agora_rtm.stop_live(send_stop=send_stop)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Stream cleanup error for %s: %s", self.device.id, err)

    async def async_ptz_ctrl(self, ptz_type: int, ptz_dir: int) -> bool:
        """Send a PTZ control command via RTM signaling.

        Requires an active live stream (RTM session must be running).
        ptz_type: 0 = single step, 1 = continuous start/stop, 2 = flip.
        ptz_dir:  -1 = left, 0 = stop, 1 = right.
        """
        rtm = await self._get_active_rtm()
        return await rtm.send_ptz_ctrl(ptz_type, ptz_dir)

    async def async_start_live_manual(self) -> bool:
        """Start RTM live signaling manually from HA controls."""
        if await get_whep_upstream_manager(self.hass).has_session(str(self.device.id)):
            LOGGER.debug(
                "Manual start_live skipped for %s: shared upstream already active",
                self.device.id,
            )
            return True

        live_feed = await self._async_get_live_feed(refresh=True)
        if live_feed is None:
            LOGGER.warning(
                "Manual start_live failed for %s: live feed token unavailable",
                self.device.id,
            )
            return False

        started = await self._agora_rtm.start_live(live_feed)
        if not started:
            LOGGER.warning(
                "Manual start_live failed for %s: RTM signaling not acknowledged",
                self.device.id,
            )
            return False

        LOGGER.debug("Manual start_live succeeded for %s", self.device.id)
        return True

    async def async_stop_live_manual(self) -> None:
        """Stop RTM live signaling manually from HA controls."""
        await get_whep_upstream_manager(self.hass).close_session(str(self.device.id))
        await self._async_close_stream(send_stop=True)
        LOGGER.debug("Manual stop_live sent for %s", self.device.id)

    def _whep_direct_url(self) -> str:
        """Return the stable direct WHEP URL exposed by Home Assistant."""
        for prefer_external in (True, False):
            try:
                base_url = get_url(self.hass, prefer_external=prefer_external)
            except NoURLAvailableError:
                continue
            return f"{base_url.rstrip('/')}/api/petkit/whep_direct/{self.device.id}"
        return f"/api/petkit/whep_direct/{self.device.id}"

    async def _async_handle_go2rtc_browser_offer(
        self,
        offer_sdp: str,
        session_id: str,
        send_message: WebRTCSendMessage,
    ) -> None:
        """Proxy one browser WebRTC session to the canonical internal go2rtc stream."""
        go2rtc_manager = get_go2rtc_stream_manager(self.hass)
        base_url = go2rtc_manager.api_base_url()
        if base_url is None:
            send_message(
                WebRTCError(
                    code="go2rtc_provider_missing",
                    message="No shared go2rtc instance is configured for this camera",
                )
            )
            return

        # Register PENDING session *before* any await so that
        # async_on_webrtc_candidate can buffer early ICE candidates.
        existing = self._go2rtc_browser_sessions.pop(session_id, None)
        session = _BrowserSession(state=_BrowserSessionState.PENDING)
        self._go2rtc_browser_sessions[session_id] = session

        if existing is not None:
            existing.state = _BrowserSessionState.CLOSED
            existing.queued_candidates.clear()
            if existing.ws_client is not None:
                await existing.ws_client.close()

        try:
            await go2rtc_manager.async_ensure_stream(self, raise_on_failure=True)
        except RuntimeError as err:
            session.state = _BrowserSessionState.FAILED
            self._go2rtc_browser_sessions.pop(session_id, None)
            send_message(
                WebRTCError(
                    code="go2rtc_stream_unavailable",
                    message=str(err),
                )
            )
            return

        if session.state in (_BrowserSessionState.CLOSED, _BrowserSessionState.FAILED):
            self._go2rtc_browser_sessions.pop(session_id, None)
            return

        ws_client = Go2RtcWsClient(
            go2rtc_manager.api_session(),
            base_url,
            source=go2rtc_manager.stream_name(str(self.device.id)),
        )

        @callback
        def on_messages(message) -> None:
            """Forward go2rtc websocket signaling back to the HA frontend."""
            match message:
                case Go2RTCCandidate():
                    send_message(
                        WebRTCCandidate(RTCIceCandidateInit(message.candidate))
                    )
                case Go2RTCAnswer():
                    send_message(WebRTCAnswer(message.sdp))
                case Go2RTCWsError():
                    send_message(
                        WebRTCError("go2rtc_webrtc_offer_failed", message.error)
                    )

        ws_client.subscribe(on_messages)
        session.ws_client = ws_client

        try:
            config = self.async_get_webrtc_client_configuration()
            await ws_client.send(
                Go2RTCOffer(offer_sdp, config.configuration.ice_servers)
            )
        except Exception as err:  # noqa: BLE001
            session.state = _BrowserSessionState.FAILED
            self._go2rtc_browser_sessions.pop(session_id, None)
            await ws_client.close()
            send_message(
                WebRTCError(
                    code="go2rtc_webrtc_offer_failed",
                    message=str(err),
                )
            )
            return

        if session.state in (_BrowserSessionState.CLOSED, _BrowserSessionState.FAILED):
            self._go2rtc_browser_sessions.pop(session_id, None)
            await ws_client.close()
            return

        # Snapshot buffered candidates, transition to ACTIVE, then flush.
        # New candidates arriving during flush go directly via ACTIVE path.
        buffered = list(session.queued_candidates)
        session.queued_candidates.clear()
        session.state = _BrowserSessionState.ACTIVE

        try:
            for candidate_str in buffered:
                await ws_client.send(Go2RTCCandidate(candidate_str))
        except Exception as err:  # noqa: BLE001
            session.state = _BrowserSessionState.FAILED
            self._go2rtc_browser_sessions.pop(session_id, None)
            await ws_client.close()
            send_message(
                WebRTCError(
                    code="go2rtc_webrtc_offer_failed",
                    message=str(err),
                )
            )

    async def _get_active_rtm(self) -> AgoraRTMSignaling:
        """Return the RTM controller for the active stream when available."""
        active_rtm = await get_whep_upstream_manager(self.hass).get_session_rtm(
            str(self.device.id)
        )
        if active_rtm is not None:
            return active_rtm
        return self._agora_rtm

    async def _async_get_live_feed(self, refresh: bool = False) -> LiveFeed | None:
        """Return current live feed token payload for this device."""
        live_feed = await self._get_live_feed()
        if live_feed is not None:
            return live_feed

        if not refresh:
            return None

        await self.coordinator.async_request_refresh()
        return await self._get_live_feed()

    async def _get_live_feed(self) -> LiveFeed | None:
        """Fetch live feed directly from API."""
        live_feed = (
            await self.coordinator.config_entry.runtime_data.client.get_live_feed(
                self.device.id
            )
        )
        if not isinstance(live_feed, LiveFeed):
            return None
        if not live_feed.channel_id or not live_feed.rtc_token:
            return None
        return live_feed

    async def async_get_live_feed(self, refresh: bool = False) -> LiveFeed | None:
        """Return the current live feed payload for shared upstream helpers."""
        return await self._async_get_live_feed(refresh=refresh)

    async def async_refresh_agora_context(
        self, live_feed: LiveFeed
    ) -> AgoraResponse | None:
        """Refresh and return the cached Agora gateway context for this camera."""
        await self._refresh_agora_context(live_feed)
        return self._agora_response

    async def _refresh_agora_context(self, live_feed: LiveFeed) -> None:
        """Fetch Agora gateway + TURN endpoints and cache ICE servers."""
        self._agora_response = None

        async with AgoraAPIClient() as agora_client:
            response = await agora_client.choose_server(
                app_id=AGORA_APP_ID,
                token=live_feed.rtc_token,
                channel_name=live_feed.channel_id,
                user_id=live_feed.uid,
                service_flags=[
                    SERVICE_IDS["CHOOSE_SERVER"],
                    SERVICE_IDS["CLOUD_PROXY_FALLBACK"],
                ],
            )

        self._agora_response = response
        ice_servers = response.get_ice_servers(use_all_turn_servers=False)
        self._ice_servers = [
            RTCIceServer(
                urls=server.urls,
                username=server.username,
                credential=server.credential,
            )
            for server in ice_servers
        ]

        LOGGER.debug(
            "Cached %d ICE servers for PetKit camera %s",
            len(self._ice_servers),
            self.device.id,
        )

    @staticmethod
    def _filter_candidates(
        candidates: list[RTCIceCandidateInit],
        agora_response: AgoraResponse,
    ) -> list[RTCIceCandidateInit]:
        """Prefer relay/srflx candidates and drop host candidates."""
        valid_ips = {addr.ip for addr in (agora_response.get_turn_addresses() or [])}

        def is_valid(cand: str) -> bool:
            if "typ srflx" in cand or "typ prflx" in cand:
                return True
            if "typ relay" in cand:
                return not valid_ips or any(ip in cand for ip in valid_ips)
            return False

        filtered = [c for c in candidates if is_valid(c.candidate or "")]

        return filtered or candidates

    def filter_agora_candidates(
        self,
        candidates: list[RTCIceCandidateInit],
        agora_response: AgoraResponse,
    ) -> list[RTCIceCandidateInit]:
        """Filter Agora ICE candidates for the shared upstream session."""
        return self._filter_candidates(candidates, agora_response)
