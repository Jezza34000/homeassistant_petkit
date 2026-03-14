"""Mirror WHEP endpoint using an internal aiortc relay."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import ipaddress
import secrets
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView

from .agora_api import SERVICE_IDS, AgoraAPIClient
from .agora_rtm import AgoraRTMSignaling
from .agora_websocket import AgoraWebSocketHandler
from .const import AGORA_APP_ID, DOMAIN, LOGGER
from .webrtc_common import (
    _add_offer_candidates,
    _get_live_feed_for_webrtc,
    _resolve_agora_user_id,
)

try:
    from aiortc import (
        RTCConfiguration,
        RTCIceServer as AiortcIceServer,
        RTCPeerConnection,
        RTCRtpSender,
        RTCSessionDescription,
    )
    from aiortc.contrib.media import MediaRelay
    from aiortc.sdp import candidate_from_sdp
except Exception as err:  # noqa: BLE001
    RTCConfiguration = None
    AiortcIceServer = None
    RTCPeerConnection = None
    RTCRtpSender = None
    RTCSessionDescription = None
    MediaRelay = None
    candidate_from_sdp = None
    AIORTC_IMPORT_ERROR = err
else:
    AIORTC_IMPORT_ERROR = None

if TYPE_CHECKING:
    from .camera import PetkitWebRTCCamera

TOKEN_REFRESH_INTERVAL_SECONDS = 20 * 60


@dataclass
class MirrorUpstreamSession:
    """One internal WebRTC ingest from Agora."""

    camera: PetkitWebRTCCamera
    peer_connection: Any
    agora_handler: AgoraWebSocketHandler
    agora_rtm: AgoraRTMSignaling
    relay: Any
    video_ready: asyncio.Event = field(default_factory=asyncio.Event)
    video_track: Any | None = None
    refresh_task: asyncio.Task[None] | None = None
    last_error: str | None = None

    @property
    def device_id(self) -> str:
        """Return device identifier."""
        return str(self.camera.device.id)

    @property
    def is_alive(self) -> bool:
        """Return whether the upstream session is ready and connected."""
        return (
            self.video_ready.is_set()
            and self.peer_connection.connectionState not in {"failed", "closed"}
        )


@dataclass
class MirrorDownstreamSession:
    """One downstream consumer served by the relay."""

    session_id: str
    kind: str
    peer_connection: Any


class PetkitMirrorRelayManager:
    """Manage internal upstream and downstream relay peers."""

    def __init__(self, hass) -> None:
        self.hass = hass
        self._lock = asyncio.Lock()
        self._upstreams: dict[str, MirrorUpstreamSession] = {}
        self._upstream_tasks: dict[str, asyncio.Task[MirrorUpstreamSession]] = {}
        self._downstreams: dict[str, dict[str, MirrorDownstreamSession]] = {}
        self._persistent_cameras: dict[str, PetkitWebRTCCamera] = {}
        self._prewarm_tasks: dict[str, asyncio.Task[None]] = {}

    def register_persistent_camera(self, camera: PetkitWebRTCCamera) -> None:
        """Keep one upstream relay alive for this camera."""
        device_id = str(camera.device.id)
        self._persistent_cameras[device_id] = camera
        self._schedule_prewarm(device_id)

    def unregister_persistent_camera(self, device_id: str) -> None:
        """Stop maintaining a hot upstream relay for this camera."""
        self._persistent_cameras.pop(device_id, None)
        task = self._prewarm_tasks.pop(device_id, None)
        if task is not None:
            task.cancel()

    async def create_downstream_offer(
        self,
        camera: PetkitWebRTCCamera,
        offer_sdp: str,
        *,
        session_id: str | None = None,
        kind: str = "whep",
    ) -> tuple[str, str]:
        """Create or reuse an upstream ingest, then answer one downstream offer."""
        device_id = str(camera.device.id)
        if kind == "whep":
            await self.close_downstreams_by_kind(device_id, kind)
        elif session_id is not None:
            await self.close_downstream(device_id, session_id)
        upstream = await self._ensure_upstream(camera)

        peer_connection = RTCPeerConnection()
        if session_id is None:
            session_id = secrets.token_hex(16)
        downstream = MirrorDownstreamSession(
            session_id=session_id,
            kind=kind,
            peer_connection=peer_connection,
        )

        @peer_connection.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            state = peer_connection.connectionState
            LOGGER.debug(
                "WHEP rebroadcast downstream %s state=%s",
                device_id,
                state,
            )
            if state in {"failed", "closed"}:
                self.hass.async_create_background_task(
                    self._handle_downstream_closed(device_id, session_id),
                    f"petkit rebroadcast close downstream {device_id}",
                )

        sender = peer_connection.addTrack(upstream.relay.subscribe(upstream.video_track))
        self._prefer_h264(peer_connection, sender)

        await peer_connection.setRemoteDescription(
            RTCSessionDescription(sdp=offer_sdp, type="offer")
        )
        answer = await peer_connection.createAnswer()
        await peer_connection.setLocalDescription(answer)
        await self._wait_for_ice_complete(peer_connection)

        async with self._lock:
            self._downstreams.setdefault(device_id, {})[session_id] = downstream

        return session_id, str(peer_connection.localDescription.sdp)

    async def close_device(self, device_id: str, *, allow_restart: bool = True) -> bool:
        """Close downstream and upstream relay state for one camera."""
        downstreams: list[MirrorDownstreamSession] = []
        upstream = None
        ensure_task = None
        async with self._lock:
            downstreams = list(self._downstreams.pop(device_id, {}).values())
            upstream = self._upstreams.pop(device_id, None)
            ensure_task = self._upstream_tasks.pop(device_id, None)

        if ensure_task is not None and ensure_task is not asyncio.current_task():
            ensure_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ensure_task

        if not downstreams and upstream is None and ensure_task is None:
            return False

        if downstreams:
            await asyncio.gather(
                *(self._shutdown_peer(d.peer_connection) for d in downstreams),
                return_exceptions=True,
            )

        if upstream is not None:
            await self._shutdown_upstream(upstream)

        if allow_restart:
            self._schedule_prewarm(device_id)

        return True

    async def close_all(self) -> None:
        """Close all relay sessions."""
        for device_id in list(self._persistent_cameras):
            self.unregister_persistent_camera(device_id)
        async with self._lock:
            device_ids = set(self._upstreams) | set(self._downstreams)
        for device_id in device_ids:
            await self.close_device(device_id, allow_restart=False)

    async def close_downstream(self, device_id: str, session_id: str) -> bool:
        """Close one downstream consumer and upstream if it was the last one."""
        async with self._lock:
            sessions = self._downstreams.get(device_id)
            downstream = sessions.pop(session_id, None) if sessions else None
            has_remaining = bool(sessions)
            keep_upstream_alive = device_id in self._persistent_cameras
            if sessions is not None and not sessions:
                self._downstreams.pop(device_id, None)

        if downstream is None:
            return False

        await self._shutdown_peer(downstream.peer_connection)
        if not has_remaining and not keep_upstream_alive:
            await self._close_upstream_if_unused(device_id)
        return True

    async def close_downstreams_by_kind(self, device_id: str, kind: str) -> bool:
        """Close downstream sessions matching kind and upstream if none remain."""
        async with self._lock:
            sessions = self._downstreams.get(device_id, {})
            matching_ids = [
                session_id
                for session_id, session in sessions.items()
                if session.kind == kind
            ]

        closed_any = False
        for session_id in matching_ids:
            closed_any = await self.close_downstream(device_id, session_id) or closed_any
        return closed_any

    async def add_downstream_candidate(
        self,
        device_id: str,
        session_id: str,
        candidate,
    ) -> bool:
        """Add trickled ICE candidate to a relay downstream peer."""
        if candidate_from_sdp is None:
            return False

        async with self._lock:
            downstream = self._downstreams.get(device_id, {}).get(session_id)

        if downstream is None:
            return False

        candidate_line = candidate.candidate or ""
        if not candidate_line:
            return True

        if candidate_line.startswith("candidate:"):
            candidate_line = candidate_line.removeprefix("candidate:")

        ice_candidate = candidate_from_sdp(candidate_line)
        ice_candidate.sdpMid = candidate.sdp_mid
        ice_candidate.sdpMLineIndex = candidate.sdp_m_line_index
        await downstream.peer_connection.addIceCandidate(ice_candidate)
        return True

    async def has_upstream(self, device_id: str) -> bool:
        """Return whether an active relay upstream already exists."""
        async with self._lock:
            upstream = self._upstreams.get(device_id)
        return upstream is not None and upstream.is_alive

    async def get_upstream_rtm(self, device_id: str) -> AgoraRTMSignaling | None:
        """Return the active upstream RTM session for one device."""
        async with self._lock:
            upstream = self._upstreams.get(device_id)
        if upstream is None or not upstream.is_alive:
            return None
        return upstream.agora_rtm

    async def _ensure_upstream(
        self,
        camera: PetkitWebRTCCamera,
    ) -> MirrorUpstreamSession:
        """Ensure an internal ingest peer is running."""
        device_id = str(camera.device.id)
        async with self._lock:
            existing = self._upstreams.get(device_id)
            if existing is not None and existing.is_alive:
                return existing
            ensure_task = self._upstream_tasks.get(device_id)
            if ensure_task is None:
                ensure_task = self.hass.async_create_task(
                    self._create_upstream(camera)
                )
                self._upstream_tasks[device_id] = ensure_task

        return await ensure_task

    async def _create_upstream(
        self,
        camera: PetkitWebRTCCamera,
    ) -> MirrorUpstreamSession:
        """Create one internal ingest peer connected to Agora."""
        device_id = str(camera.device.id)
        async with self._lock:
            existing = self._upstreams.get(device_id)
        if existing is not None:
            await self.close_device(device_id, allow_restart=False)

        try:
            return await self._build_upstream(camera)
        finally:
            async with self._lock:
                current_task = self._upstream_tasks.get(device_id)
                if current_task is asyncio.current_task():
                    self._upstream_tasks.pop(device_id, None)

    async def _build_upstream(
        self,
        camera: PetkitWebRTCCamera,
    ) -> MirrorUpstreamSession:
        """Negotiate the upstream aiortc ingest peer against Agora."""
        device_id = str(camera.device.id)

        live_feed = await _get_live_feed_for_webrtc(camera)
        if live_feed is None:
            raise RuntimeError("Live feed unavailable or missing RTM credentials")

        agora_user_id = _resolve_agora_user_id(camera, live_feed)
        async with AgoraAPIClient() as agora_client:
            agora_response = await agora_client.choose_server(
                app_id=AGORA_APP_ID,
                token=live_feed.rtc_token,
                channel_name=live_feed.channel_id,
                user_id=agora_user_id,
                service_flags=[
                    SERVICE_IDS["CHOOSE_SERVER"],
                    SERVICE_IDS["CLOUD_PROXY_FALLBACK"],
                ],
            )

        ice_servers = []
        for server in agora_response.get_ice_servers(use_all_turn_servers=False):
            ice_servers.append(
                AiortcIceServer(
                    urls=server.urls,
                    username=server.username,
                    credential=server.credential,
                )
            )

        peer_connection = RTCPeerConnection(
            RTCConfiguration(iceServers=ice_servers)
        )
        relay = MediaRelay()
        upstream = MirrorUpstreamSession(
            camera=camera,
            peer_connection=peer_connection,
            agora_handler=AgoraWebSocketHandler(
                rtc_token_provider=camera._refresh_rtc_token,
                prefer_instant_video=True,
                subscribe_retry_delay=1.0,
                subscribe_retry_attempts=3,
            ),
            agora_rtm=AgoraRTMSignaling(AGORA_APP_ID),
            relay=relay,
        )

        @peer_connection.on("track")
        def on_track(track: Any) -> None:
            LOGGER.debug(
                "WHEP rebroadcast upstream %s track kind=%s",
                device_id,
                track.kind,
            )
            if track.kind == "video" and upstream.video_track is None:
                upstream.video_track = track
                upstream.video_ready.set()

            @track.on("ended")
            async def on_ended() -> None:
                LOGGER.debug(
                    "WHEP rebroadcast upstream %s track ended kind=%s",
                    device_id,
                    track.kind,
                )
                self.hass.async_create_background_task(
                    self.close_device(device_id),
                    f"petkit rebroadcast close device {device_id}",
                )

        @peer_connection.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            state = peer_connection.connectionState
            LOGGER.debug("WHEP rebroadcast upstream %s state=%s", device_id, state)
            if state in {"failed", "closed"}:
                upstream.last_error = f"upstream connection state={state}"
                self.hass.async_create_background_task(
                    self.close_device(device_id),
                    f"petkit rebroadcast close device {device_id}",
                )

        transceiver = peer_connection.addTransceiver("video", direction="recvonly")
        self._prefer_h264_transceiver(transceiver)

        offer = await peer_connection.createOffer()
        await peer_connection.setLocalDescription(offer)
        await self._wait_for_ice_complete(peer_connection)

        parsed_candidates = _add_offer_candidates(
            upstream.agora_handler,
            str(peer_connection.localDescription.sdp),
        )
        upstream.agora_handler.candidates = camera._filter_candidates(
            upstream.agora_handler.candidates,
            agora_response,
        )
        LOGGER.debug(
            "WHEP rebroadcast upstream %s candidates=%d filtered=%d",
            device_id,
            parsed_candidates,
            len(upstream.agora_handler.candidates),
        )

        rtm_started = await upstream.agora_rtm.start_live(live_feed)
        if not rtm_started:
            LOGGER.debug(
                "WHEP rebroadcast upstream %s RTM start_live not acknowledged",
                device_id,
            )

        answer_sdp = await upstream.agora_handler.connect_and_join(
            live_feed=live_feed,
            offer_sdp=str(peer_connection.localDescription.sdp),
            session_id=secrets.token_hex(16),
            app_id=AGORA_APP_ID,
            agora_response=agora_response,
        )
        if not answer_sdp:
            await asyncio.gather(
                upstream.agora_handler.disconnect(),
                upstream.agora_rtm.stop_live(send_stop=True),
                self._shutdown_peer(peer_connection),
                return_exceptions=True,
            )
            raise RuntimeError("Agora upstream negotiation failed")

        await peer_connection.setRemoteDescription(
            RTCSessionDescription(sdp=answer_sdp, type="answer")
        )
        await asyncio.wait_for(upstream.video_ready.wait(), timeout=20)
        upstream.refresh_task = self.hass.async_create_background_task(
            self._refresh_tokens(upstream),
            f"petkit rebroadcast refresh tokens {device_id}",
        )

        async with self._lock:
            self._upstreams[device_id] = upstream

        return upstream

    async def _refresh_tokens(self, upstream: MirrorUpstreamSession) -> None:
        """Refresh RTM tokens while the upstream session is alive."""
        try:
            while True:
                await asyncio.sleep(TOKEN_REFRESH_INTERVAL_SECONDS)
                live_feed = await _get_live_feed_for_webrtc(upstream.camera)
                if live_feed is None:
                    continue
                await upstream.agora_rtm.update_tokens(live_feed)
        except asyncio.CancelledError:
            return

    async def _handle_downstream_closed(
        self,
        device_id: str,
        session_id: str,
    ) -> None:
        """Cleanup after downstream closure."""
        await self.close_downstream(device_id, session_id)

    async def _close_upstream_if_unused(self, device_id: str) -> None:
        """Close upstream relay only when no downstream consumers remain."""
        async with self._lock:
            if self._downstreams.get(device_id):
                return
            upstream = self._upstreams.pop(device_id, None)

        if upstream is None:
            return

        await self._shutdown_upstream(upstream)

    def _schedule_prewarm(self, device_id: str, delay: float = 0) -> None:
        """Start or restart background upstream prewarm for a persistent camera."""
        if device_id not in self._persistent_cameras:
            return

        task = self._prewarm_tasks.get(device_id)
        if task is not None and not task.done() and task is not asyncio.current_task():
            return

        prewarm_task = self.hass.async_create_background_task(
            self._prewarm_upstream(device_id, delay),
            f"petkit rebroadcast prewarm {device_id}",
        )
        self._prewarm_tasks[device_id] = prewarm_task

        def _cleanup(done_task: asyncio.Task[None]) -> None:
            if self._prewarm_tasks.get(device_id) is done_task:
                self._prewarm_tasks.pop(device_id, None)

        prewarm_task.add_done_callback(_cleanup)

    async def _prewarm_upstream(self, device_id: str, delay: float) -> None:
        """Warm the upstream relay in the background."""
        if delay > 0:
            await asyncio.sleep(delay)

        camera = self._persistent_cameras.get(device_id)
        if camera is None:
            return

        try:
            await self._ensure_upstream(camera)
            LOGGER.debug("WHEP rebroadcast prewarmed for %s", device_id)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("WHEP rebroadcast prewarm failed for %s: %s", device_id, err)
            self._schedule_prewarm(device_id, delay=15)

    async def _shutdown_upstream(self, upstream: MirrorUpstreamSession) -> None:
        """Cancel refresh task and tear down one upstream session."""
        if upstream.refresh_task is not None:
            upstream.refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await upstream.refresh_task

        await asyncio.gather(
            upstream.agora_handler.disconnect(),
            upstream.agora_rtm.stop_live(send_stop=True),
            self._shutdown_peer(upstream.peer_connection),
            return_exceptions=True,
        )

    @staticmethod
    async def _shutdown_peer(peer_connection: Any) -> None:
        """Close one aiortc peer connection."""
        with contextlib.suppress(Exception):  # noqa: BLE001
            await peer_connection.close()

    @staticmethod
    async def _wait_for_ice_complete(peer_connection: Any) -> None:
        """Wait briefly for ICE gathering to finish."""
        if peer_connection.iceGatheringState == "complete":
            return

        ice_complete = asyncio.Event()

        @peer_connection.on("icegatheringstatechange")
        async def on_icegatheringstatechange() -> None:
            if peer_connection.iceGatheringState == "complete":
                ice_complete.set()

        await asyncio.wait_for(ice_complete.wait(), timeout=5)

    @staticmethod
    def _prefer_h264(peer_connection: Any, sender: Any) -> None:
        """Prefer H264 for downstream consumers when available."""
        try:
            transceiver = next(
                transceiver
                for transceiver in peer_connection.getTransceivers()
                if transceiver.sender == sender
            )
        except StopIteration:
            return

        PetkitMirrorRelayManager._prefer_h264_transceiver(transceiver)

    @staticmethod
    def _prefer_h264_transceiver(transceiver: Any) -> None:
        """Restrict codec preferences to H264 when supported."""
        if RTCRtpSender is None:
            return
        try:
            h264_codecs = [
                codec
                for codec in RTCRtpSender.getCapabilities("video").codecs
                if codec.mimeType == "video/H264"
            ]
            if h264_codecs:
                transceiver.setCodecPreferences(h264_codecs)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Failed to set H264 codec preference: %s", err)


def _get_manager(hass) -> PetkitMirrorRelayManager:
    """Return the shared rebroadcast manager."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    manager = domain_data.get("whep_mirror_manager")
    if manager is None:
        manager = PetkitMirrorRelayManager(hass)
        domain_data["whep_mirror_manager"] = manager
    return manager


async def async_cleanup_whep_mirror_sessions(hass) -> None:
    """Close all active rebroadcast sessions."""
    manager = hass.data.get(DOMAIN, {}).pop("whep_mirror_manager", None)
    if manager is not None:
        await manager.close_all()


def _check_external_auth(request: web.Request) -> web.Response | None:
    """Allow authenticated HA users or explicit access tokens."""
    hass = request.app["hass"]
    if request.get("hass_user"):
        return None

    token = request.query.get("token")
    if token:
        if hass.auth.async_validate_access_token(token) is None:
            return web.Response(status=401, text="Invalid token")
        return None

    return web.Response(status=401, text="Authentication required")


def _is_loopback_request(request: web.Request) -> bool:
    """Return whether the request originates from localhost."""
    remote = request.remote
    if remote:
        with contextlib.suppress(ValueError):
            return ipaddress.ip_address(remote).is_loopback

    peername = None
    if request.transport is not None:
        peername = request.transport.get_extra_info("peername")

    if isinstance(peername, tuple) and peername:
        host = peername[0]
        with contextlib.suppress(ValueError):
            return ipaddress.ip_address(host).is_loopback

    return False


def _check_internal_auth(request: web.Request) -> web.Response | None:
    """Allow only loopback requests for the internal rebroadcast endpoint."""
    if _is_loopback_request(request):
        return None
    return web.Response(status=403, text="Internal endpoint is loopback-only")


class _BasePetkitWhepMirrorView(HomeAssistantView):
    """Shared WHEP rebroadcast endpoint logic."""

    requires_auth = False
    _downstream_kind = "whep"

    def _check_auth(self, request: web.Request) -> web.Response | None:
        """Validate request authentication for this endpoint."""
        raise NotImplementedError

    async def _post_impl(self, request: web.Request, device_id: str) -> web.Response:
        """Receive SDP offer, relay via an internal aiortc peer, return answer."""
        hass = request.app["hass"]

        auth_error = self._check_auth(request)
        if auth_error is not None:
            return auth_error

        cameras = hass.data.get(DOMAIN, {}).get("cameras", {})
        camera = cameras.get(device_id)
        if camera is None:
            return web.Response(status=404, text="Camera not found")

        offer_sdp = await request.text()
        if not offer_sdp or not offer_sdp.strip():
            return web.Response(status=400, text="Empty SDP offer")

        if AIORTC_IMPORT_ERROR is not None:
            return web.Response(
                status=503,
                text=f"aiortc relay unavailable: {AIORTC_IMPORT_ERROR}",
            )

        manager = _get_manager(hass)

        try:
            _, answer_sdp = await manager.create_downstream_offer(
                camera,
                offer_sdp,
                kind=self._downstream_kind,
            )
        except asyncio.TimeoutError:
            LOGGER.error("WHEP rebroadcast timed out for %s", device_id)
            return web.Response(status=504, text="Timed out waiting for upstream video")
        except (OSError, RuntimeError, ValueError) as err:
            LOGGER.error("WHEP rebroadcast failed for %s: %s", device_id, err)
            return web.Response(status=502, text=str(err))

        response = web.StreamResponse(
            status=201,
            headers={
                "Content-Type": "application/sdp",
                "Location": request.path,
            },
        )
        await response.prepare(request)
        await response.write(answer_sdp.encode())
        await response.write_eof()
        return response

    async def _delete_impl(self, request: web.Request, device_id: str) -> web.Response:
        """Tear down an active rebroadcast session."""
        hass = request.app["hass"]

        auth_error = self._check_auth(request)
        if auth_error is not None:
            return auth_error

        manager = _get_manager(hass)
        if not await manager.close_downstreams_by_kind(
            device_id, self._downstream_kind
        ):
            return web.Response(status=404, text="No active rebroadcast session")

        return web.Response(status=200, text="Session closed")


class PetkitWhepMirrorView(_BasePetkitWhepMirrorView):
    """Public WHEP endpoint for external rebroadcast consumers."""

    url = "/api/petkit/whep_mirror/{device_id}"
    name = "api:petkit:whep_mirror"
    _downstream_kind = "whep"

    def _check_auth(self, request: web.Request) -> web.Response | None:
        """Allow HA user auth or token query auth."""
        return _check_external_auth(request)

    async def post(self, request: web.Request, device_id: str) -> web.Response:
        """Receive SDP offer, relay via an internal aiortc peer, return answer."""
        return await self._post_impl(request, device_id)

    async def delete(self, request: web.Request, device_id: str) -> web.Response:
        """Tear down an active rebroadcast session."""
        return await self._delete_impl(request, device_id)


class PetkitInternalWhepMirrorView(_BasePetkitWhepMirrorView):
    """Loopback-only WHEP endpoint for HA-managed internal rebroadcast consumers."""

    url = "/api/petkit/whep_internal/{device_id}"
    name = "api:petkit:whep_internal"
    _downstream_kind = "internal"

    def _check_auth(self, request: web.Request) -> web.Response | None:
        """Allow loopback requests only."""
        return _check_internal_auth(request)

    async def post(self, request: web.Request, device_id: str) -> web.Response:
        """Receive SDP offer for an internal consumer."""
        return await self._post_impl(request, device_id)

    async def delete(self, request: web.Request, device_id: str) -> web.Response:
        """Tear down an active internal rebroadcast session."""
        return await self._delete_impl(request, device_id)
