"""Media Source Implementation."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import re

from custom_components.petkit.const import (
    CONF_MEDIA_PATH,
    COORDINATOR,
    DEFAULT_MEDIA_PATH,
    DOMAIN,
    LOGGER,
    MEDIA_ROOT,
    MEDIA_SECTION,
)
from homeassistant.components.media_player import (
    MediaClass,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
)
from homeassistant.core import HomeAssistant

EXT_MP4 = ".mp4"
EXT_JPG = ".jpg"


async def async_get_media_source(hass: HomeAssistant) -> PetkitMediaSource:
    """Set up Petkit media source."""
    return PetkitMediaSource(hass)


class PetkitMediaSource(MediaSource):
    """Provide Petkit media source recordings."""

    name: str = "Petkit"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize PetkitMediaSource."""
        super().__init__(DOMAIN)
        self.hass = hass
        self.coordinator = self.get_coordinator()
        self.media_path = Path(
            self.coordinator.config_entry.options.get(MEDIA_SECTION, {}).get(
                CONF_MEDIA_PATH, DEFAULT_MEDIA_PATH
            )
        )

    def get_coordinator(self):
        """Retrieve the integration's coordinator."""
        if DOMAIN in self.hass.data and COORDINATOR in self.hass.data[DOMAIN]:
            return self.hass.data[DOMAIN][COORDINATOR]
        LOGGER.error("Petkit coordinator not found in hass.data.")
        return None

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve media to a URL/path."""
        file_path = self.media_path / Path(item.identifier)
        if not file_path.exists():
            raise ValueError(f"File not found: {file_path}")
        LOGGER.debug(f"Media Source: Resolving media {file_path}")

        url = async_process_play_media_url(
            self.hass,
            f"{MEDIA_ROOT}{file_path}",
            allow_relative_url=True,
            for_supervisor_network=True,
        )
        mime_type = self.get_mime_type(file_path.suffix)
        return PlayMedia(url, mime_type)

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse the media source."""
        identifier = item.identifier or str(self.media_path)
        current_path = self.media_path / Path(identifier)

        if not current_path.exists() or not current_path.is_dir():
            raise ValueError(f"Invalid path: {current_path}")
        LOGGER.debug(f"Media Source: Browsing {current_path}")

        children = await asyncio.to_thread(self._get_children_from_path, current_path)

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=str(current_path),
            title=DOMAIN.capitalize(),
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.PLAYLIST,
            can_expand=True,
            can_play=False,
            children=children,
        )

    def _get_children_from_path(self, path: Path):
        """Get children from a path."""
        children = []
        for child in sorted(path.iterdir()):
            LOGGER.debug(f"Media Source: Processing child {child.name} in {path}")
            if child.is_dir():
                title = self.get_device_name_from_data(
                    self.convert_date(child.name)
                ).capitalize()

                if title.lower() == "snapshot":
                    media_class = MediaClass.IMAGE
                elif title.lower() == "video":
                    media_class = MediaClass.VIDEO
                else:
                    media_class = MediaClass.DIRECTORY

                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=str(child.relative_to(self.media_path)),
                        title=title,
                        media_class=media_class,
                        media_content_type=MediaType.VIDEO,
                        can_expand=True,
                        can_play=False,
                    )
                )
            elif child.is_file():
                children.append(self._build_file_media_item(child))
        return children

    def _build_file_media_item(self, child: Path) -> BrowseMediaSource:
        """Build a file media item."""
        snapshot_parent = child.parent.relative_to(Path("/")).with_name("snapshot")
        thumbnail_path = (
            Path(MEDIA_ROOT) / snapshot_parent / child.name.replace(EXT_MP4, EXT_JPG)
        ).as_posix()

        thumbnail_url = async_process_play_media_url(
            self.hass,
            thumbnail_path,
            allow_relative_url=True,
            for_supervisor_network=True,
        )
        media_class = self.get_media_class(child.suffix)
        media_type = self.get_media_type(child.suffix)

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=str(child.relative_to(self.media_path)),
            title=self.extract_timestamp_and_convert(child.name),
            media_class=media_class,
            media_content_type=media_type,
            thumbnail=thumbnail_url,
            can_expand=False,
            can_play=True,
        )

    def get_device_name_from_data(self, match_device: str) -> str:
        """Match a string with a key in the data dictionary and extract the device name."""
        data = self.coordinator.data or {}
        for key, value in data.items():
            if match_device in str(key):
                return value.device_nfo.device_name.capitalize()
        return match_device

    @staticmethod
    def convert_date(input_string: str) -> str:
        """Convert a string in the format YYYYMMDD to DD/MM/YYYY."""
        match = re.fullmatch(r"\d{8}", input_string)
        if not match:
            return input_string

        try:
            date_obj = datetime.strptime(input_string, "%Y%m%d")
            return date_obj.strftime("%d/%m/%Y")
        except ValueError:
            return input_string

    @staticmethod
    def extract_timestamp_and_convert(filename: str) -> str:
        """Extract the timestamp from a filename and convert it to HH:MM:SS."""
        try:
            timestamp_str = filename.split("_")[1].split(".")[0]
            timestamp = int(timestamp_str)
            time_obj = datetime.fromtimestamp(timestamp).time()
            return time_obj.strftime("%H:%M:%S")
        except (IndexError, ValueError):
            return filename

    @staticmethod
    def get_media_class(extension: str) -> str:
        """Return the media class based on the file extension."""
        if extension == EXT_MP4:
            return MediaClass.VIDEO
        if extension == EXT_JPG:
            return MediaClass.IMAGE
        return MediaClass.APP

    @staticmethod
    def get_media_type(extension: str) -> str:
        """Return the media type based on the file extension."""
        if extension == EXT_MP4:
            return MediaType.VIDEO
        if extension == EXT_JPG:
            return MediaType.IMAGE
        return MediaType.APP

    @staticmethod
    def get_mime_type(extension: str) -> str:
        """Get MIME type for a given file extension."""
        mime_types = {
            EXT_MP4: "video/mp4",
            EXT_JPG: "image/jpeg",
        }
        return mime_types.get(extension, "application/octet-stream")
