"""Race orchestrator.

One race at a time (asyncio.Lock -> 409), two sides racing concurrently:
each side gets its own TaskGroup + Semaphore so a slow LLM never throttles
Jev. Per-ticket wall clock is bounded by asyncio.timeout; per-attempt HTTP
timeouts (12s) and max_retries=1 live in the adapters so a retry is never
severed mid-flight.

Events flow into a per-run ring buffer (byte-bounded, monotonic seq used as
the SSE id) and fan out to live subscriber queues. Reconnects replay from
the buffer; if the requested position was evicted, an explicit `gap` event
is emitted rather than silently skipping.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

from app.schemas import Correct, Ticket, Usage, Verdict
from app.scoring import SideTally, grade, winner
from app.security import scrub

BUFFER_MAX_BYTES = 256 * 1024
RUN_TTL_SECONDS = 15 * 60
QUEUE_MAX = 1024

CostFn = Callable[[Usage], float | None]


@dataclass
class RaceRun:
    id: str
    total: int
    sides: dict[str, str]  # side -> provider label (e.g. {"jev": "jev-latest", "llm": "claude-sonnet-5"})
    status: str = "running"  # running | done | cancelled | error
    created: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    task: asyncio.Task | None = None
    tallies: dict[str, SideTally] = field(default_factory=dict)
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


class RaceManager:
    def __init__(self) -> None:
        self._runs: dict[str, RaceRun] = {}
        # The slot is claimed synchronously in start(), not inside the spawned
        # task. An asyncio.Lock acquired by the task would not be held yet when
        # a second request arrives in the same tick, so both would be admitted
        # and the races would queue up instead of the second being refused —
        # quietly doubling the spend this guard exists to cap.
        self._active_race_id: str | None = None

    def get(self, race_id: str) -> RaceRun | None:
        self._purge_expired()
        return self._runs.get(race_id)

    def _purge_expired(self) -> None:
        now = time.monotonic()
        for rid in list(self._runs):
            run = self._runs[rid]
            if run.is_finished and run.finished_at and now - run.finished_at > RUN_TTL_SECONDS:
                del self._runs[rid]

    @property
    def race_in_progress(self) -> bool:
        return self._active_race_id is not None

    async def start(
        self,
        tickets: list[Ticket],
        classifiers: dict[str, object],  # side -> Classifier
        cost_fns: dict[str, CostFn],
        concurrency: dict[str, int],
        per_ticket_timeout: float,
        sides_meta: dict[str, str],
    ) -> RaceRun | None:
        """Returns the new run, or None if a race is already in progress."""
        self._purge_expired()
        if self._active_race_id is not None:
            return None
        run = RaceRun(
            id=secrets.token_urlsafe(16),
            total=len(tickets),
            sides=sides_meta,
            tallies={side: SideTally() for side in classifiers},
        )
        # Claimed before the first await, so a second caller in this same tick
        # is refused rather than queued behind us.
        self._active_race_id = run.id
        self._runs[run.id] = run
        run.task = asyncio.create_task(
            self._run_race(run, tickets, classifiers, cost_fns, concurrency, per_ticket_timeout)
        )
        return run

    def _release(self, run: RaceRun, status: str | None = None) -> None:
        """Idempotent teardown. Safe to call from the race task's finally and
        from cancel(), because a task cancelled before its first execution
        never reaches its own finally block."""
        if status and run.status == "running":
            run.status = status
            run.publish(status, {"totals": {s: t.summary() for s, t in run.tallies.items()}})
        if run.finished_at is None:
            run.finished_at = time.monotonic()
        if self._active_race_id == run.id:
            self._active_race_id = None

    async def cancel(self, race_id: str) -> bool:
        run = self.get(race_id)
        if run is None:
            return False
        if run.task and not run.task.done():
            run.task.cancel()
            try:
                await run.task
            except asyncio.CancelledError:
                pass
        # Backstop: if the task was cancelled before it ever ran, its own
        # finally never fired, so the slot would leak and the stream would
        # never see a terminal event.
        self._release(run, status="cancelled")
        return True

    async def _run_race(
        self,
        run: RaceRun,
        tickets: list[Ticket],
        classifiers: dict[str, object],
        cost_fns: dict[str, CostFn],
        concurrency: dict[str, int],
        per_ticket_timeout: float,
    ) -> None:
        try:
            started = time.perf_counter()
            run.publish("start", {"race_id": run.id, "total": run.total, "sides": run.sides})
            try:
                async with asyncio.TaskGroup() as tg:
                    for side, classifier in classifiers.items():
                        tg.create_task(
                            self._run_side(
                                run,
                                side,
                                classifier,
                                tickets,
                                cost_fns[side],
                                concurrency.get(side, 4),
                                per_ticket_timeout,
                            )
                        )
                run.status = "done"
                run.publish(
                    "race_done",
                    {
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                        "totals": {s: t.summary() for s, t in run.tallies.items()},
                        "winner": winner(run.tallies["jev"], run.tallies["llm"])
                        if {"jev", "llm"} <= run.tallies.keys()
                        else None,
                    },
                )
            except asyncio.CancelledError:
                run.status = "cancelled"
                run.publish("cancelled", {"totals": {s: t.summary() for s, t in run.tallies.items()}})
                raise
            except Exception:
                run.status = "error"
                run.publish("race_error", {"error": "internal_error"})
        finally:
            # Always release the slot, including on cancellation, or the app
            # would refuse every later race until it restarts.
            self._release(run)

    async def _run_side(
        self,
        run: RaceRun,
        side: str,
        classifier,
        tickets: list[Ticket],
        cost_fn: CostFn,
        limit: int,
        per_ticket_timeout: float,
    ) -> None:
        sem = asyncio.Semaphore(limit)
        tally = run.tallies[side]

        async def one(index: int, ticket: Ticket) -> None:
            async with sem:
                try:
                    async with asyncio.timeout(per_ticket_timeout):
                        verdict: Verdict = await classifier.classify(ticket.text)
                except TimeoutError:
                    verdict = Verdict(error="timeout")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    verdict = Verdict(error="upstream_error")
            correct = grade(ticket, verdict)
            cost = cost_fn(verdict.usage) if verdict.error is None else None
            tally.add(correct, verdict, cost)
            if verdict.error is not None:
                run.publish(
                    "error",
                    {"side": side, "index": index, "ticket_id": ticket.id, "kind": verdict.error},
                )
            else:
                run.publish(
                    "result",
                    {
                        "side": side,
                        "index": index,
                        "ticket_id": ticket.id,
                        "latency_ms": round(verdict.latency_ms, 1),
                        "verdict": {
                            "urgent_p": verdict.urgent_p,
                            "team": verdict.team,
                            "frustration": verdict.frustration,
                            # Null for the LLM: it has no confidence to gate
                            # on, which is what the routing panel shows.
                            "team_confidence": verdict.team_confidence,
                        },
                        "correct": correct.model_dump(),
                        "usage": verdict.usage.model_dump(),
                        "cost_usd": cost,
                    },
                )

        async with asyncio.TaskGroup() as tg:
            for i, ticket in enumerate(tickets):
                tg.create_task(one(i, ticket))
        run.publish("done", {"side": side, "totals": tally.summary()})


manager = RaceManager()
