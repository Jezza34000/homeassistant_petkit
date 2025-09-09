"""Camera platform for Petkit Smart Devices integration."""

from __future__ import annotations

from dataclasses import dataclass

from pypetkitapi import FEEDER_WITH_CAMERA, LITTER_WITH_CAMERA, Feeder, Litter

from homeassistant.components.camera import (
    Camera,
    CameraEntityDescription,
    CameraEntityFeature,
    StreamType,
    WebRTCAnswer,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .agora_wss import AgoraWebSocketHandler
from .const import LOGGER
from .coordinator import PetkitDataUpdateCoordinator
from .data import PetkitConfigEntry, PetkitDevices
from .entity import PetKitDescSensorBase, PetkitEntity


@dataclass(frozen=True, kw_only=True)
class PetKitCameraDesc(PetKitDescSensorBase, CameraEntityDescription):
    """Petkit camera with Agora streaming."""

    event_key: str | None = None


COMMON_ENTITIES = []

CAMERA_MAPPING: dict[type[PetkitDevices], list[PetKitCameraDesc]] = {
    Feeder: [
        *COMMON_ENTITIES,
        PetKitCameraDesc(
            key="Live camera feed",
            event_key=None,
            translation_key="live_camera_feed",
            only_for_types=FEEDER_WITH_CAMERA,
        ),
    ],
    Litter: [
        *COMMON_ENTITIES,
        PetKitCameraDesc(
            key="Live camera feed",
            event_key=None,
            translation_key="live_camera_feed",
            only_for_types=LITTER_WITH_CAMERA,
        ),
    ],
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetkitConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Camera using config entry."""
    devices = entry.runtime_data.client.petkit_entities.values()
    entities = [
        PetkitCamera(
            hass=hass,
            coordinator=entry.runtime_data.coordinator,
            entity_description=entity_description,
            device=device,
        )
        for device in devices
        for device_type, entity_descriptions in CAMERA_MAPPING.items()
        if isinstance(device, device_type)
        for entity_description in entity_descriptions
        if entity_description.is_supported(device)
    ]
    LOGGER.debug(
        "CAMERA : Adding %s (on %s available)",
        len(entities),
        sum(len(descriptors) for descriptors in CAMERA_MAPPING.values()),
    )
    async_add_entities(entities)


class PetkitCamera(PetkitEntity, Camera):
    """Petkit WebRTC camera entity, compatible with Agora WebRTC."""

    entity_description: PetKitCameraDesc
    _attr_has_entity_name = True
    _attr_is_streaming = True
    _attr_supported_features = CameraEntityFeature.STREAM
    _supports_native_async_webrtc = True

    def __init__(
        self,
        coordinator: PetkitDataUpdateCoordinator,
        entity_description: PetKitCameraDesc,
        hass: HomeAssistant,
        device: PetkitDevices,
    ) -> None:
        """Initialize PetkitCamera"""
        PetkitEntity.__init__(self, coordinator, device)
        Camera.__init__(self)
        self.coordinator = coordinator
        self.entity_description = entity_description
        self.device = device
        self.hass = hass
        self._attr_name = f"{device.device_nfo.device_name} Camera"
        self._agora_handler = AgoraWebSocketHandler(hass)

    @property
    def unique_id(self) -> str:
        """Return a unique ID for the binary_sensor."""
        return f"{self.device.device_nfo.device_type}_{self.device.sn}_{self.entity_description.key}"

    @property
    def frontend_stream_type(self) -> StreamType | None:
        """Return the type of stream supported by this camera."""
        return StreamType.WEB_RTC

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a placeholder image for WebRTC cameras (no snapshot yet)."""
        return None

    def _make_agora_subscription(self):
        """Fetch live data from the device stream"""
        device_data = self.coordinator.data.get(self.device.id)
        if not device_data:
            return None
        live_feed = getattr(device_data, "live_feed", None)
        if not live_feed:
            return None
        return live_feed

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Handle WebRTC offer, initiate negotiation with Agora backend.

        This function intercepts the WebRTC request initiated from the HA UI and uses the Agora handler to negotiate the SDP answer
        """
        LOGGER.info(
            "Handling WebRTC offer for Petkit device %s session %s",
            self.device.name,
            session_id,
        )
        try:
            stream_data = self._make_agora_subscription()
            if not stream_data:
                LOGGER.error(
                    "No Agora.io subscription stream data for device %s", self.device.name
                )
                send_message(WebRTCError("500", "No stream data for camera"))
                return

            answer_sdp = await self._agora_handler.connect_and_join(
                stream_data, offer_sdp, session_id
            )

            if answer_sdp:
                send_message(WebRTCAnswer(answer_sdp))
                LOGGER.info(
                    "WebRTC negotiation completed OK for Petkit device %s", self.device.name
                )
            else:
                send_message(WebRTCError("500", "WebRTC negotiation failed"))
        except Exception as ex:
           LOGGER.error("Error handling WebRTC offer for %s: %r", self.device.name, ex)
           send_message(WebRTCError("500", f"Error handling WebRTC offer: {ex}"))

    async def async_on_webrtc_candidate(self, session_id: str, candidate: dict) -> None:
        """Ignore WebRTC ICE candidates (handled by Agora, not us)."""
        return

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Close WebRTC session if needed (optional for Agora)."""
        return
