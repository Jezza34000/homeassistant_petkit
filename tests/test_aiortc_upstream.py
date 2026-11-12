"""Lifecycle tests for the aiortc upstream stream (no Home Assistant, no network)."""

# ruff: noqa: D102, D107  (fakes mirror aiortc / manager APIs)

from __future__ import annotations

import asyncio
import importlib.util
import io
import itertools
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "custom_components" / "petkit"
PKG = "petkit_aiortc_under_test"


def _stub(name: str, **attrs) -> None:
    module = ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module


try:
    import homeassistant.components.http  # noqa: F401
except ImportError:
    for name in ("homeassistant", "homeassistant.components"):
        _stub(name)
    _stub("homeassistant.components.http", HomeAssistantView=object)

_stub(PKG, __path__=[str(PKG_DIR)])
_stub(f"{PKG}.const", DOMAIN="petkit", LOGGER=logging.getLogger(PKG))


class FakeUpstream:
    """Stands in for PetkitAgoraUpstreamManager."""

    def __init__(self) -> None:
        self.created = 0
        self.closed = 0
        self.fail = False

    async def create_session(self, camera, offer_sdp):
        self.created += 1
        if self.fail:
            raise RuntimeError("agora down")
        return "session", "answer"

    async def close_session(self, device_id):
        self.closed += 1


UPSTREAM = FakeUpstream()
_stub(
    f"{PKG}.whep_proxy",
    _check_external_auth=None,
    get_whep_upstream_manager=lambda hass: UPSTREAM,
)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{PKG}.{name}", PKG_DIR / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load("h264_fanout")
aiortc_upstream = _load("aiortc_upstream")


class FakePeerConnection:
    """Records what the stream does with its peer connection."""

    instances: list[FakePeerConnection] = []

    def __init__(self, configuration) -> None:
        self.connectionState = "new"
        self.closed = False
        self.localDescription = None
        FakePeerConnection.instances.append(self)

    def addTransceiver(self, kind, direction):
        return SimpleNamespace(receiver=SimpleNamespace())

    def on(self, event):
        return lambda handler: handler

    async def createOffer(self):
        return SimpleNamespace(sdp="offer", type="offer")

    async def setLocalDescription(self, description) -> None:
        self.localDescription = description

    async def setRemoteDescription(self, description) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    monkeypatch.setattr(aiortc_upstream, "RTCPeerConnection", FakePeerConnection)
    FakePeerConnection.instances.clear()
    UPSTREAM.created = UPSTREAM.closed = 0
    UPSTREAM.fail = False


def _stream():
    camera = SimpleNamespace(device=SimpleNamespace(id=1))
    return aiortc_upstream.PetkitAiortcStream(SimpleNamespace(), camera)


def test_one_session_shared_and_stopped_by_last_subscriber() -> None:
    """Two subscribers share one Agora session; the last one stops it."""

    async def run() -> None:
        stream = _stream()
        first = await stream.async_subscribe()
        second = await stream.async_subscribe()
        assert UPSTREAM.created == 1

        await stream.async_unsubscribe(first)
        assert UPSTREAM.closed == 0

        await stream.async_unsubscribe(second)
        assert UPSTREAM.closed == 1
        assert FakePeerConnection.instances[0].closed

    asyncio.run(run())


def test_failed_start_leaves_nothing_running() -> None:
    """A failed Agora join closes everything and the next subscriber retries."""

    async def run() -> None:
        stream = _stream()
        UPSTREAM.fail = True
        with pytest.raises(RuntimeError):
            await stream.async_subscribe()
        assert stream.fanout.subscriber_count == 0
        assert UPSTREAM.closed == 1
        assert FakePeerConnection.instances[0].closed

        UPSTREAM.fail = False
        await stream.async_subscribe()
        assert UPSTREAM.created == 2

    asyncio.run(run())


def test_mpegts_muxer_keeps_camera_timestamps() -> None:
    """Every access unit comes out at once, with strictly increasing timestamps."""
    av = pytest.importorskip("av")
    sps = bytes.fromhex(
        "0000000127640033ad00ce804202169a808080f800000300080000030191898003d090"
    )
    frames = [sps + b"\x00\x00\x00\x01\x65" + bytes(200)] + [
        b"\x00\x00\x00\x01\x41" + bytes(50)
    ] * 3
    timestamps = [0, 3600, 3600, 10800]  # one duplicate, as after a gap

    muxer = aiortc_upstream.MpegTsMuxer()
    chunks = [
        muxer.mux(frame, ts) for frame, ts in zip(frames, timestamps, strict=True)
    ]
    muxer.close()
    assert all(chunks)

    demuxed = av.open(io.BytesIO(b"".join(chunks)), format="mpegts")
    pts = [p.pts for p in demuxed.demux(video=0) if p.size]
    assert len(pts) == 4
    assert all(b > a for a, b in itertools.pairwise(pts))
    assert pts[1] - pts[0] == 3600
