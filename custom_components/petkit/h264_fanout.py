"""Fan-out of timestamped H.264 Annex-B access units to streaming subscribers."""

from __future__ import annotations

import asyncio

# (Annex-B access unit, 90 kHz timestamp); None ends the stream.
type Frame = tuple[bytes, int]

_NAL_TYPE_SPS = 7
_START_CODE = b"\x00\x00\x01"


def sps_offset(access_unit: bytes) -> int | None:
    """Return the offset of the start code preceding the first SPS NAL unit."""
    index = access_unit.find(_START_CODE)
    while index >= 0:
        header = index + len(_START_CODE)
        if header < len(access_unit) and access_unit[header] & 0x1F == _NAL_TYPE_SPS:
            # Keep the 4-byte start code form when present.
            return index - 1 if index > 0 and access_unit[index - 1] == 0 else index
        index = access_unit.find(_START_CODE, header)
    return None


class H264Fanout:
    """Deliver access units to every subscriber, each starting on an SPS."""

    def __init__(self, max_queue: int = 100) -> None:
        """Initialize an empty subscriber set."""
        self._max_queue = max_queue
        # Subscriber queue -> True while it still waits for a decodable start.
        self._subscribers: dict[asyncio.Queue[Frame | None], bool] = {}

    @property
    def subscriber_count(self) -> int:
        """Return the number of active subscribers."""
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue[Frame | None]:
        """Register a subscriber; it receives data from the next SPS onwards."""
        queue: asyncio.Queue[Frame | None] = asyncio.Queue(self._max_queue)
        self._subscribers[queue] = True
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Frame | None]) -> None:
        """Forget a subscriber."""
        self._subscribers.pop(queue, None)

    def publish(self, access_unit: bytes, timestamp: int) -> None:
        """Queue one access unit and its 90 kHz timestamp for every subscriber."""
        offset: int | None = -1  # computed lazily, only when someone waits
        for queue, waiting in self._subscribers.items():
            data = access_unit
            if waiting:
                if offset == -1:
                    offset = sps_offset(access_unit)
                if offset is None:
                    continue
                data = access_unit[offset:]
            if queue.full():
                # Slow reader: drop its backlog and restart it on the next SPS.
                while not queue.empty():
                    queue.get_nowait()
                self._subscribers[queue] = True
                continue
            queue.put_nowait((data, timestamp))
            self._subscribers[queue] = False

    def close(self) -> None:
        """Signal end of stream to all current subscribers."""
        for queue in self._subscribers:
            while queue.full():
                queue.get_nowait()
            queue.put_nowait(None)
        self._subscribers.clear()


class FramePacer:
    """Spread frames out in time the way the camera produced them.

    go2rtc stamps raw H.264 by arrival time. After a lost packet is recovered
    the jitter buffer releases several frames at once, which would give them
    almost the same timestamp; pacing by the 90 kHz frame timestamps keeps the
    arrival times (and so the recording) even.
    """

    def __init__(self, latency: float = 0.3, max_drift: float = 2.0) -> None:
        """Delay frames by `latency` s; resync after a gap or jump of `max_drift` s."""
        self._latency = latency
        self._max_drift = max_drift
        self._base: tuple[float, int] | None = None

    def delay(self, timestamp: int, now: float) -> float:
        """Return how long to wait before sending the frame stamped `timestamp`."""
        if self._base is not None:
            base_time, base_ts = self._base
            wait = base_time + (timestamp - base_ts) / 90000 - now
            if -self._max_drift < wait < self._max_drift:
                return max(wait, 0.0)
        # First frame, stall or timestamp jump: restart the schedule here.
        self._base = (now + self._latency, timestamp)
        return self._latency
