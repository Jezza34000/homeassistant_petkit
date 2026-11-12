"""Receive the PetKit/Agora video with aiortc and expose it as MPEG-TS.

go2rtc's WebRTC client has no jitter buffer and drops RTX retransmissions, so
every lost packet corrupts a keyframe (green video, undecodable recordings).
aiortc reorders packets, NACKs losses and accepts RTX, then hands complete
access units to go2rtc through a plain HTTP MPEG-TS stream. MPEG-TS carries
the camera's own timestamps; a raw H.264 stream would make go2rtc stamp frames
by arrival time, which breaks recordings when the jitter buffer releases a
burst of frames (non-monotonic DTS in the mp4 muxer).
"""

from __future__ import annotations

import asyncio
import contextlib
from fractions import Fraction
import io
from typing import TYPE_CHECKING, Any

from aiohttp import web
from aiortc import (
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
    rtcrtpreceiver,
)
from aiortc.jitterbuffer import JitterBuffer
from aiortc.mediastreams import MediaStreamError
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
import av

from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN, LOGGER
from .h264_fanout import Frame, H264Fanout
from .whep_proxy import _check_external_auth, get_whep_upstream_manager

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .camera import PetkitWebRTCCamera

# End the HTTP stream when no access unit arrives for this long (stalled session).
_IDLE_TIMEOUT_SECONDS = 30

# aiortc waits for a missing packet until 512 newer ones arrive (~17 s here);
# Agora never resends some gaps (e.g. right after joining). 128 packets still
# hold a full keyframe (~60 packets) plus reordering, but skip a gap in ~4 s.
_VIDEO_JITTER_CAPACITY = 128


_VIDEO_TIME_BASE = Fraction(1, 90000)


class _PassthroughDecoder:
    """Return the depacketized frame (data + 90 kHz timestamp), not a picture."""

    def decode(self, encoded_frame: Any) -> list[Any]:
        return [encoded_frame]


class MpegTsMuxer:
    """Wrap timestamped Annex-B access units into MPEG-TS bytes."""

    def __init__(self) -> None:
        """Open an in-memory MPEG-TS muxer with one H.264 stream."""
        self._out = io.BytesIO()
        self._container = av.open(
            self._out,
            mode="w",
            format="mpegts",
            container_options={"flush_packets": "1"},
        )
        self._stream = self._container.add_stream("h264")
        self._stream.time_base = _VIDEO_TIME_BASE
        self._last_dts: int | None = None

    def mux(self, access_unit: bytes, timestamp: int) -> bytes:
        """Return the MPEG-TS bytes for one access unit (may be empty)."""
        # Equal or older timestamps would be rejected by the muxer.
        if self._last_dts is not None and timestamp <= self._last_dts:
            timestamp = self._last_dts + 1
        self._last_dts = timestamp
        packet = av.Packet(access_unit)
        packet.stream = self._stream
        packet.time_base = _VIDEO_TIME_BASE
        packet.pts = packet.dts = timestamp
        self._container.mux(packet)
        data = self._out.getvalue()
        self._out.seek(0)
        self._out.truncate()
        return data

    def close(self) -> None:
        """Release the muxer."""
        with contextlib.suppress(av.FFmpegError):
            self._container.close()


def _patch_aiortc() -> None:
    """Hand H.264 access units to us instead of decoding them (idempotent)."""
    if getattr(rtcrtpreceiver, "_petkit_patched", False):
        return

    original_get_decoder = rtcrtpreceiver.get_decoder

    # shortcut: process-wide patch; fine while no other HA component decodes
    # H.264 through aiortc, replace with a per-receiver hook if one appears.
    def get_decoder(codec: RTCRtpCodecParameters) -> Any:
        if codec.name == "H264":
            return _PassthroughDecoder()
        return original_get_decoder(codec)

    rtcrtpreceiver.get_decoder = get_decoder
    rtcrtpreceiver._petkit_patched = True  # noqa: SLF001


class PetkitAiortcStream:
    """One on-demand aiortc session per camera, shared by HTTP subscribers."""

    def __init__(self, hass: HomeAssistant, camera: PetkitWebRTCCamera) -> None:
        """Initialize idle state."""
        self.hass = hass
        self.camera = camera
        self.fanout = H264Fanout()
        self._lock = asyncio.Lock()
        self._pc: RTCPeerConnection | None = None
        self._pump_task: asyncio.Task[None] | None = None

    @property
    def _device_id(self) -> str:
        return str(self.camera.device.id)

    async def async_subscribe(self) -> asyncio.Queue[Frame | None]:
        """Subscribe to the stream, starting the Agora session if needed."""
        async with self._lock:
            queue = self.fanout.subscribe()
            if self._pc is None:
                try:
                    await self._async_start()
                except Exception:
                    self.fanout.unsubscribe(queue)
                    await self._async_stop()
                    raise
            return queue

    async def async_unsubscribe(self, queue: asyncio.Queue[Frame | None]) -> None:
        """Unsubscribe and stop the session once nobody listens."""
        async with self._lock:
            self.fanout.unsubscribe(queue)
            if not self.fanout.subscriber_count:
                await self._async_stop()

    async def _async_start(self) -> None:
        _patch_aiortc()
        # Agora gateways are ICE-lite with public addresses: host candidates suffice.
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self._pc = pc
        video = pc.addTransceiver("video", direction="recvonly")
        # shortcut: replaces a private attribute; revisit on aiortc upgrades.
        video.receiver._RTCRtpReceiver__jitter_buffer = JitterBuffer(  # noqa: SLF001
            capacity=_VIDEO_JITTER_CAPACITY, is_video=True
        )
        pc.addTransceiver("audio", direction="recvonly")

        @pc.on("track")
        def on_track(track) -> None:
            if track.kind == "video" and self._pump_task is None:
                self._pump_task = self.hass.async_create_background_task(
                    self._async_pump(track),
                    f"petkit aiortc pump {self._device_id}",
                )

        @pc.on("connectionstatechange")
        def on_state() -> None:
            LOGGER.debug(
                "PetKit aiortc %s connection state: %s",
                self._device_id,
                pc.connectionState,
            )
            if pc.connectionState in ("failed", "closed"):
                # Ending all subscribers makes go2rtc reconnect with a fresh session.
                self.fanout.close()

        await pc.setLocalDescription(await pc.createOffer())
        _, answer_sdp = await get_whep_upstream_manager(self.hass).create_session(
            self.camera, pc.localDescription.sdp
        )
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer_sdp, type="answer")
        )
        LOGGER.debug("PetKit aiortc session started for %s", self._device_id)

    async def _async_pump(self, track) -> None:
        try:
            while True:
                frame = await track.recv()
                self.fanout.publish(frame.data, frame.timestamp)
        except MediaStreamError:
            pass
        finally:
            self.fanout.close()

    async def _async_stop(self) -> None:
        pc, self._pc = self._pc, None
        task, self._pump_task = self._pump_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if pc is None:
            return
        await pc.close()
        await get_whep_upstream_manager(self.hass).close_session(self._device_id)
        LOGGER.debug("PetKit aiortc session stopped for %s", self._device_id)


def get_aiortc_stream(
    hass: HomeAssistant, camera: PetkitWebRTCCamera
) -> PetkitAiortcStream:
    """Return the shared aiortc stream for one camera."""
    streams = hass.data.setdefault(DOMAIN, {}).setdefault("aiortc_streams", {})
    device_id = str(camera.device.id)
    if (stream := streams.get(device_id)) is None or stream.camera is not camera:
        stream = streams[device_id] = PetkitAiortcStream(hass, camera)
    return stream


async def async_cleanup_aiortc_streams(hass: HomeAssistant) -> None:
    """Stop every aiortc session (integration unload)."""
    streams = hass.data.get(DOMAIN, {}).pop("aiortc_streams", {})
    for stream in streams.values():
        stream.fanout.close()
        async with stream._lock:  # noqa: SLF001
            await stream._async_stop()  # noqa: SLF001


class PetkitH264StreamView(HomeAssistantView):
    """MPEG-TS H.264 stream consumed by the shared go2rtc instance."""

    url = "/api/petkit/h264/{device_id}"
    name = "api:petkit:h264"
    requires_auth = False

    async def get(self, request: web.Request, device_id: str) -> web.StreamResponse:
        """Stream access units until the client leaves or the session dies."""
        if (auth_error := _check_external_auth(request)) is not None:
            return auth_error

        hass = request.app["hass"]
        camera = hass.data.get(DOMAIN, {}).get("cameras", {}).get(device_id)
        if camera is None:
            return web.Response(status=404, text="Camera not found")

        stream = get_aiortc_stream(hass, camera)
        try:
            queue = await stream.async_subscribe()
        except Exception as err:  # noqa: BLE001
            LOGGER.error("PetKit aiortc upstream failed for %s: %s", device_id, err)
            return web.Response(status=502, text=str(err))

        response = web.StreamResponse(headers={"Content-Type": "video/mp2t"})
        muxer = MpegTsMuxer()
        try:
            await response.prepare(request)
            while (
                frame := await asyncio.wait_for(queue.get(), _IDLE_TIMEOUT_SECONDS)
            ) is not None:
                if data := muxer.mux(*frame):
                    await response.write(data)
        except (TimeoutError, ConnectionResetError) as err:
            LOGGER.debug("PetKit H.264 stream %s ended: %r", device_id, err)
        finally:
            muxer.close()
            await stream.async_unsubscribe(queue)
        return response
