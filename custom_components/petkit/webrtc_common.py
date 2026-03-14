"""Shared helpers for PetKit WebRTC signaling flows."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pypetkitapi import TEMP_CAMERA_TYPES
from webrtc_models import RTCIceCandidateInit

from .const import LOGGER

if TYPE_CHECKING:
    from .agora_websocket import AgoraWebSocketHandler
    from .camera import PetkitWebRTCCamera


def _missing_live_feed_fields(live_feed) -> list[str]:
    """Return missing fields needed to start and sustain a WebRTC stream."""
    required_fields = {
        "channel_id": getattr(live_feed, "channel_id", None),
        "rtc_token": getattr(live_feed, "rtc_token", None),
        "app_rtm_user_id": getattr(live_feed, "app_rtm_user_id", None),
        "dev_rtm_user_id": getattr(live_feed, "dev_rtm_user_id", None),
        "rtm_token": getattr(live_feed, "rtm_token", None),
    }
    return [field for field, value in required_fields.items() if not value]


def _live_feed_ready_for_webrtc(live_feed) -> bool:
    """Return whether a live feed has all fields required for WebRTC."""
    return live_feed is not None and not _missing_live_feed_fields(live_feed)


def _resolve_agora_user_id(camera: PetkitWebRTCCamera, live_feed) -> int:
    """Pick the most reliable Agora uid available for choose_server."""
    if (live_feed_uid := getattr(live_feed, "uid", None)) not in (None, ""):
        try:
            return int(live_feed_uid)
        except (TypeError, ValueError):
            LOGGER.debug("WebRTC: invalid live_feed uid=%s", live_feed_uid)

    client = camera.coordinator.config_entry.runtime_data.client
    session_user_id = getattr(getattr(client, "_session", None), "user_id", None)
    if session_user_id not in (None, ""):
        try:
            return int(str(session_user_id))
        except (TypeError, ValueError):
            LOGGER.debug("WebRTC: invalid session user_id=%s", session_user_id)

    app_rtm_user_id = str(getattr(live_feed, "app_rtm_user_id", "") or "")
    digits = "".join(char for char in app_rtm_user_id if char.isdigit())
    if digits:
        return int(digits)

    return 0


def _add_offer_candidates(
    handler: AgoraWebSocketHandler,
    offer_sdp: str,
) -> int:
    """Extract inline ICE candidates from an SDP offer."""
    seen_candidates = {
        candidate.candidate for candidate in handler.candidates if candidate.candidate
    }
    added = 0
    media_index = -1
    current_mid: str | None = None

    for raw_line in offer_sdp.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("m="):
            media_index += 1
            current_mid = None
            continue

        if line.startswith("a=mid:"):
            current_mid = line.removeprefix("a=mid:")
            continue

        if not line.startswith("a=candidate:"):
            continue

        candidate_line = line.removeprefix("a=")
        if candidate_line in seen_candidates:
            continue

        handler.add_ice_candidate(
            RTCIceCandidateInit(
                candidate=candidate_line,
                sdp_mid=current_mid,
                sdp_m_line_index=media_index if media_index >= 0 else None,
            )
        )
        seen_candidates.add(candidate_line)
        added += 1

    return added


async def _get_live_feed_for_webrtc(camera: PetkitWebRTCCamera):
    """Fetch live feed data and wake supported cameras if needed."""
    live_feed = await camera.async_get_live_feed()
    if _live_feed_ready_for_webrtc(live_feed):
        return live_feed

    device_id = camera.device.id
    missing_fields = _missing_live_feed_fields(live_feed)
    if missing_fields:
        LOGGER.debug(
            "WebRTC: initial live feed for %s missing %s",
            device_id,
            ", ".join(missing_fields),
        )

    await camera.coordinator.async_request_refresh()
    live_feed = await camera.async_get_live_feed()
    if _live_feed_ready_for_webrtc(live_feed):
        return live_feed

    device_type = str(
        getattr(getattr(camera.device, "device_nfo", None), "device_type", "") or ""
    ).lower()
    if device_type not in TEMP_CAMERA_TYPES:
        LOGGER.debug(
            "WebRTC: device %s (%s) does not support temporary_open_camera",
            device_id,
            device_type,
        )
        return None

    client = camera.coordinator.config_entry.runtime_data.client
    LOGGER.debug(
        "WebRTC: requesting temporary_open_camera for %s (%s)",
        device_id,
        device_type,
    )
    try:
        await client.temporary_open_camera(device_type, device_id)
    except Exception as err:  # noqa: BLE001
        LOGGER.debug(
            "WebRTC: temporary_open_camera failed for %s: %s",
            device_id,
            err,
        )
        return None

    await asyncio.sleep(3)
    await camera.coordinator.async_request_refresh()
    live_feed = await camera.async_get_live_feed()
    if _live_feed_ready_for_webrtc(live_feed):
        return live_feed

    LOGGER.debug(
        "WebRTC: live feed for %s still missing %s after temporary_open_camera",
        device_id,
        ", ".join(_missing_live_feed_fields(live_feed)),
    )
    return None
