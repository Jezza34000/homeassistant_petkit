"""Download litter waste-check photos that pypetkitapi 1.31.1 skips.

pypetkitapi indexes downloaded media by (event_id, media_type). A litter
visit's toileting photo and its waste-check photo share the same event_id,
and PetKit uploads the waste-check photo minutes after the visit, so once the
toileting photo is on disk the waste-check photo counts as "already
downloaded" and is never fetched. Index by event type as well.
"""

from __future__ import annotations

from pypetkitapi.const import MediaType
from pypetkitapi.media import MediaCloud, MediaFile, MediaManager


def _key(event_id: str, event_type, media_type: MediaType) -> tuple:
    return (event_id, event_type, media_type)


def _add_media_to_table(self: MediaManager, media_file: MediaFile) -> None:
    self.media_table.append(media_file)
    self._media_index[
        _key(media_file.event_id, media_file.event_type, media_file.media_type)
    ] = media_file


def _rebuild_index(self: MediaManager) -> None:
    self._media_index.clear()
    for media_file in self.media_table:
        self._media_index[
            _key(media_file.event_id, media_file.event_type, media_file.media_type)
        ] = media_file


def _is_media_type_missing(
    self: MediaManager,
    media_cloud: MediaCloud,
    dl_types: list[MediaType],
    media_type: MediaType,
) -> bool:
    source = media_cloud.image if media_type == MediaType.IMAGE else media_cloud.video
    return bool(
        source
        and media_type in dl_types
        and _key(media_cloud.event_id, media_cloud.event_type, media_type)
        not in self._media_index
    )


def patch_media_manager() -> None:
    """Make MediaManager tell toileting and waste-check photos apart.

    shortcut: monkeypatch of pypetkitapi 1.31.1 internals; drop it once
    pypetkitapi keys its media index by event type.
    """
    MediaManager._add_media_to_table = _add_media_to_table  # noqa: SLF001
    MediaManager._rebuild_index = _rebuild_index  # noqa: SLF001
    MediaManager._is_media_type_missing = _is_media_type_missing  # noqa: SLF001
