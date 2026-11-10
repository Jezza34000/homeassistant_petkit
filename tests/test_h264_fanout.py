"""Unit tests for the H.264 fan-out helper (no Home Assistant)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
_PATH = ROOT / "custom_components" / "petkit" / "h264_fanout.py"
_spec = importlib.util.spec_from_file_location("petkit_h264_fanout", _PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules["petkit_h264_fanout"] = _mod
_spec.loader.exec_module(_mod)

H264Fanout = _mod.H264Fanout
sps_offset = _mod.sps_offset

SC4 = b"\x00\x00\x00\x01"
IDR = SC4 + b"\x67sps" + SC4 + b"\x68pps" + SC4 + b"\x65idr"
P = SC4 + b"\x41p"


def test_sps_offset() -> None:
    """Locate the SPS start code."""
    assert sps_offset(IDR) == 0
    assert sps_offset(P) is None
    assert sps_offset(SC4 + b"\x06sei" + IDR) == 8
    assert sps_offset(b"\x00\x00\x01\x27sps") == 0  # 3-byte start code, ref_idc 1


def test_subscriber_starts_on_sps_and_gets_following_frames() -> None:
    """New subscribers start on a keyframe."""
    fanout = H264Fanout()
    queue = fanout.subscribe()
    fanout.publish(P)
    fanout.publish(SC4 + b"\x06sei" + IDR)
    fanout.publish(P)
    assert [queue.get_nowait(), queue.get_nowait()] == [IDR, P]
    assert queue.empty()


def test_slow_subscriber_restarts_on_next_sps() -> None:
    """Overflowing subscribers resync on the next keyframe."""
    fanout = H264Fanout(max_queue=2)
    queue = fanout.subscribe()
    for frame in (IDR, P, P, P):
        fanout.publish(frame)
    assert queue.empty()  # backlog dropped on overflow
    fanout.publish(P)
    assert queue.empty()  # still waiting for a keyframe
    fanout.publish(IDR)
    assert queue.get_nowait() == IDR


def test_close_ends_subscribers_and_allows_new_ones() -> None:
    """Closing ends current subscribers only."""
    fanout = H264Fanout(max_queue=1)
    queue = fanout.subscribe()
    fanout.publish(IDR)
    fanout.close()
    assert queue.get_nowait() is None
    assert fanout.subscriber_count == 0
    fresh = fanout.subscribe()
    fanout.publish(IDR)
    assert fresh.get_nowait() == IDR
