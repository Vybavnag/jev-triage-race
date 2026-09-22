"""Per-run event buffer and the SSE generator, shared by races and reviews.

Events flow into a per-run ring buffer (byte-bounded, monotonic seq used as
the SSE id) and fan out to live subscriber queues. Reconnects replay from
the buffer; if the requested position was evicted, an explicit `gap` event
is emitted rather than silently skipping. Runs live in memory and expire a
while after they finish.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.security import scrub

BUFFER_MAX_BYTES = 256 * 1024
RUN_TTL_SECONDS = 15 * 60
QUEUE_MAX = 1024
HEARTBEAT_SECONDS = 15


@dataclass
class EventRun:
    id: str
    status: str = "running"  # running | done | cancelled | error
    created: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    task: asyncio.Task | None = None
    _seq: int = 0
    _buffer: list[tuple[int, str, dict]] = field(default_factory=list)  # (seq, event, data)
    _buffer_bytes: int = 0
    floor_seq: int = 0  # oldest seq still in buffer
    _subscribers: list[asyncio.Queue] = field(default_factory=list)

    def publish(self, event: str, data: dict) -> None:
        self._seq += 1
        data = scrub(data)
        entry = (self._seq, event, data)
        size = len(json.dumps(data, default=str).encode()) + len(event)
        self._buffer.append(entry)
        self._buffer_bytes += size
        while self._buffer and self._buffer_bytes > BUFFER_MAX_BYTES:
            old_seq, old_event, old_data = self._buffer.pop(0)
            self._buffer_bytes -= (
                len(json.dumps(old_data, default=str).encode()) + len(old_event)
            )
            self.floor_seq = old_seq
        for q in list(self._subscribers):
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass  # slow consumer: it will resync via Last-Event-ID replay

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def replay_from(self, last_id: int) -> tuple[bool, list[tuple[int, str, dict]]]:
        """Events after last_id. Returns (gapped, events)."""
        gapped = last_id < self.floor_seq
        return gapped, [e for e in self._buffer if e[0] > last_id]

    @property
    def is_finished(self) -> bool:
        return self.status in ("done", "cancelled", "error")


def purge_expired(runs: dict[str, EventRun]) -> None:
    now = time.monotonic()
    for rid in list(runs):
        run = runs[rid]
        if run.is_finished and run.finished_at and now - run.finished_at > RUN_TTL_SECONDS:
            del runs[rid]


def sse_frame(seq: int, event: str, data: dict) -> str:
    return f"id: {seq}\nevent: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def sse_events(
    run: EventRun, last_id_header: str | None, terminal: frozenset[str]
) -> AsyncIterator[str]:
    """Replay what the client missed, then follow the run live until its
    terminal event. Pings keep proxies from closing an idle stream."""
    try:
        last_id = int(last_id_header) if last_id_header else 0
    except ValueError:
        last_id = 0
    queue = run.subscribe()
    try:
        gapped, backlog = run.replay_from(last_id)
        if gapped:
            yield sse_frame(run.floor_seq, "gap", {"from": last_id, "to": run.floor_seq})
        replayed_to = last_id
        for seq, event, data in backlog:
            yield sse_frame(seq, event, data)
            replayed_to = seq
            if event in terminal:
                return
        if run.is_finished:
            return
        while True:
            try:
                seq, event, data = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if seq <= replayed_to:
                continue
            yield sse_frame(seq, event, data)
            if event in terminal:
                return
    finally:
        run.unsubscribe(queue)
