"""Race orchestrator.

One race at a time (asyncio.Lock -> 409), two sides racing concurrently:
each side gets its own TaskGroup + Semaphore so a slow LLM never throttles
Jev. Per-ticket wall clock is bounded by asyncio.timeout; per-attempt HTTP
timeouts (12s) and max_retries=1 live in the adapters so a retry is never
severed mid-flight.

Events go through the shared per-run buffer in app.streaming. Nothing here
judges an answer: bundled tickets travel with their expected labels in the
`start` event so the UI can show each answer beside what the label says, and
the person decides. Pasted tickets carry no labels and are never echoed.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.schemas import RaceItem, Ticket, Usage, Verdict
from app.scoring import SideTally, winner
from app.streaming import BUFFER_MAX_BYTES, EventRun, purge_expired  # noqa: F401 - re-exported

CostFn = Callable[[Usage], float | None]


def ticket_view(item: RaceItem) -> dict:
    """What the client learns about a ticket before any answer arrives. A
    bundled ticket brings its text and expected labels; a pasted one is an
    id only, because the client already holds its own text and frames must
    never echo it."""
    if isinstance(item, Ticket):
        return {
            "id": item.id,
            "text": item.text,
            "expected": {"urgent": item.urgent, "team": item.team, "frustration": item.frustration},
        }
    return {"id": item.id, "text": None, "expected": None}


@dataclass(kw_only=True)
class RaceRun(EventRun):
    total: int
    sides: dict[str, str]  # side -> provider label (e.g. {"jev": "jev-latest", "llm": "claude-sonnet-5"})
    labeled: bool = True
    tallies: dict[str, SideTally] = field(default_factory=dict)
    # The provider clients built from the visitor's keys for this run alone;
    # closed exactly once when the run ends, however it ends.
    providers: object | None = None

    def terminal_payload(self) -> dict:
        """What every terminal event carries: per-side totals."""
        return {"totals": {s: t.summary() for s, t in self.tallies.items()}}


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
        purge_expired(self._runs)
        return self._runs.get(race_id)

    @property
    def race_in_progress(self) -> bool:
        return self._active_race_id is not None

    async def start(
        self,
        tickets: Sequence[RaceItem],
        classifiers: dict[str, object],  # side -> Classifier
        cost_fns: dict[str, CostFn],
        concurrency: dict[str, int],
        per_ticket_timeout: float,
        sides_meta: dict[str, str],
        providers: object | None = None,
    ) -> RaceRun | None:
        """Returns the new run, or None if a race is already in progress."""
        purge_expired(self._runs)
        if self._active_race_id is not None:
            return None
        run = RaceRun(
            id=secrets.token_urlsafe(16),
            total=len(tickets),
            sides=sides_meta,
            labeled=all(isinstance(t, Ticket) for t in tickets),
            tallies={side: SideTally() for side in classifiers},
            providers=providers,
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
            run.publish(status, run.terminal_payload())
        if run.finished_at is None:
            run.finished_at = time.monotonic()
        if self._active_race_id == run.id:
            self._active_race_id = None

    @staticmethod
    async def _close_providers(run: RaceRun) -> None:
        providers, run.providers = run.providers, None  # once, whoever gets here first
        if providers is not None:
            await providers.close()

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
        await self._close_providers(run)
        return True

    async def _run_race(
        self,
        run: RaceRun,
        tickets: Sequence[RaceItem],
        classifiers: dict[str, object],
        cost_fns: dict[str, CostFn],
        concurrency: dict[str, int],
        per_ticket_timeout: float,
    ) -> None:
        try:
            started = time.perf_counter()
            run.publish(
                "start",
                {
                    "race_id": run.id,
                    "total": run.total,
                    "sides": run.sides,
                    "labeled": run.labeled,
                    "tickets": [ticket_view(t) for t in tickets],
                },
            )
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
                        **run.terminal_payload(),
                        "winner": winner(run.tallies["jev"], run.tallies["llm"])
                        if {"jev", "llm"} <= run.tallies.keys()
                        else None,
                    },
                )
            except asyncio.CancelledError:
                run.status = "cancelled"
                run.publish("cancelled", run.terminal_payload())
                raise
            except Exception:
                run.status = "error"
                run.publish("race_error", {"error": "internal_error"})
        finally:
            # Always release the slot, including on cancellation, or the app
            # would refuse every later race until it restarts.
            self._release(run)
            await self._close_providers(run)

    async def _run_side(
        self,
        run: RaceRun,
        side: str,
        classifier,
        tickets: Sequence[RaceItem],
        cost_fn: CostFn,
        limit: int,
        per_ticket_timeout: float,
    ) -> None:
        sem = asyncio.Semaphore(limit)
        tally = run.tallies[side]

        async def one(index: int, ticket: RaceItem) -> None:
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
            cost = cost_fn(verdict.usage) if verdict.error is None else None
            tally.add(verdict, cost)
            if verdict.error is not None:
                run.publish(
                    "error",
                    {"side": side, "index": index, "ticket_id": ticket.id, "kind": verdict.error},
                )
                return
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
                        # Null for the LLM: it asserts a team with nothing
                        # to say how close the call was.
                        "team_confidence": verdict.team_confidence,
                    },
                    "usage": verdict.usage.model_dump(),
                    "cost_usd": cost,
                },
            )

        async with asyncio.TaskGroup() as tg:
            for i, ticket in enumerate(tickets):
                tg.create_task(one(i, ticket))
        run.publish("done", {"side": side, "totals": tally.summary()})


manager = RaceManager()
