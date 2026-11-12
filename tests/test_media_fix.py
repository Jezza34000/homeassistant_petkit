"""Waste-check photos must download even when the toileting photo exists."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

pypetkitapi_media = pytest.importorskip("pypetkitapi.media")
from pypetkitapi.const import MediaType, RecordType  # noqa: E402

MediaCloud = pypetkitapi_media.MediaCloud
MediaFile = pypetkitapi_media.MediaFile
MediaManager = pypetkitapi_media.MediaManager

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "petkit_media_fix", ROOT / "custom_components" / "petkit" / "media_fix.py"
)
assert _spec and _spec.loader
media_fix = importlib.util.module_from_spec(_spec)
sys.modules["petkit_media_fix"] = media_fix
_spec.loader.exec_module(media_fix)

EVENT = "20008815_1794431408"


def _cloud(event_type: RecordType) -> MediaCloud:
    return MediaCloud(
        event_id=EVENT,
        event_type=event_type,
        device_id=20008815,
        user_id=1,
        image="https://example.invalid/picture",
        video=None,
        filepath="20008815/20261112/x",
        aes_key="k",
        timestamp=1794431408,
    )


async def _missing(manager: MediaManager) -> list[RecordType]:
    manager._add_media_to_table(  # noqa: SLF001
        MediaFile(
            event_id=EVENT,
            device_id=20008815,
            timestamp=1794431408,
            media_type=MediaType.IMAGE,
            event_type=RecordType.TOILETING,
            full_file_path=Path("toileting/snapshot") / f"{EVENT}.jpg",
        )
    )
    missing = await manager.list_missing_files(
        [_cloud(RecordType.TOILETING), _cloud(RecordType.WASTE_CHECK)],
        dl_type=[MediaType.IMAGE],
        event_type=[RecordType.TOILETING, RecordType.WASTE_CHECK],
    )
    return [m.event_type for m in missing]


@pytest.mark.asyncio
async def test_waste_check_photo_is_downloaded_after_toileting_photo() -> None:
    """Only the waste-check photo is missing once the toileting photo is on disk."""
    media_fix.patch_media_manager()
    assert await _missing(MediaManager()) == [RecordType.WASTE_CHECK]
