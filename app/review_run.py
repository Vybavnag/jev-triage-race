"""Review orchestrator: one block of code, both sides at once, streamed.

A review is a two-lane race. Each side runs in its own task and publishes
its answer the moment it lands, so a watcher sees Jev finish while the LLM
is still thinking. At most `ReviewSlots.limit` reviews run at a time,
process-wide, and a caller that finds no slot is refused rather than queued.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from app.classifiers import base
from app.deps import ReviewSlots, review_slots
from app.schemas import ReviewSide, ReviewVerdict, Usage
from app.streaming import EventRun, purge_expired

CostFn = Callable[[Usage], float | None]


async def guarded(call: Awaitable[ReviewVerdict], timeout: float) -> ReviewVerdict:
    """Bound one side's wall clock and turn any failure into a verdict, so a
    slow or broken provider costs that side its answer, never the run."""
    try:
        async with asyncio.timeout(timeout):
            return await call
    except TimeoutError:
        return ReviewVerdict(error=base.TIMEOUT)
    except Exception:
        return ReviewVerdict(error=base.UPSTREAM_ERROR)


@dataclass(kw_only=True)
class ReviewRun(EventRun):
    questions: list[str]
    sides: dict[str, str]  # side -> provider label
    results: dict[str, ReviewSide] = field(default_factory=dict)
    slot_held: bool = True
    # The provider clients built from the visitor's keys for this run alone;
    # closed exactly once when the run ends, however it ends.
    providers: object | None = None


class ReviewManager:
    def __init__(self, slots: ReviewSlots) -> None:
        self._runs: dict[str, ReviewRun] = {}
        self._slots = slots

    def get(self, review_id: str) -> ReviewRun | None:
        purge_expired(self._runs)
        return self._runs.get(review_id)

    async def start(
        self,
        *,
        code: str,
        questions: Sequence[str],
        reviewers: dict[str, object],  # side -> Reviewer
        cost_fns: dict[str, CostFn],
        sides_meta: dict[str, str],
        attempt_timeout: float | None,
        timeout: float,
        providers: object | None = None,
    ) -> ReviewRun | None:
        """Returns the new run, or None when every slot is taken."""
        purge_expired(self._runs)
        # Claimed before the first await, so two callers in the same tick
        # cannot both be admitted past the cap.
        if not self._slots.try_acquire():
            return None
        run = ReviewRun(
            id=secrets.token_urlsafe(16),
            questions=list(questions),
            sides=sides_meta,
            providers=providers,
        )
        self._runs[run.id] = run
        run.task = asyncio.create_task(
            self._run(run, code, reviewers, cost_fns, attempt_timeout, timeout)
        )
        return run

    def _release(self, run: ReviewRun, status: str | None = None) -> None:
        """Idempotent teardown, callable from the task's finally and from
        cancel(): a task cancelled before it ever ran never reaches its own
        finally, so the slot would leak and the stream would never end."""
        if status and run.status == "running":
            run.status = status
            run.publish(status, {})
        if run.finished_at is None:
            run.finished_at = time.monotonic()
        if run.slot_held:
            run.slot_held = False
            self._slots.release()

    @staticmethod
    async def _close_providers(run: ReviewRun) -> None:
        providers, run.providers = run.providers, None  # once, whoever gets here first
        if providers is not None:
            await providers.close()

    async def cancel(self, review_id: str) -> bool:
        run = self.get(review_id)
        if run is None:
            return False
        if run.task and not run.task.done():
            run.task.cancel()
            try:
                await run.task
            except asyncio.CancelledError:
                pass
        self._release(run, status="cancelled")
        await self._close_providers(run)
        return True

    async def _run(
        self,
        run: ReviewRun,
        code: str,
        reviewers: dict[str, object],
        cost_fns: dict[str, CostFn],
        attempt_timeout: float | None,
        timeout: float,
    ) -> None:
        try:
            started = time.perf_counter()
            run.publish(
                "start", {"review_id": run.id, "questions": run.questions, "sides": run.sides}
            )
            try:
                async with asyncio.TaskGroup() as tg:
                    for side, reviewer in reviewers.items():
                        tg.create_task(
                            self._side(run, side, reviewer, code, cost_fns[side], attempt_timeout, timeout)
                        )
                run.status = "done"
                run.publish(
                    "review_done", {"elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}
                )
            except asyncio.CancelledError:
                run.status = "cancelled"
                run.publish("cancelled", {})
                raise
            except Exception:
                run.status = "error"
                run.publish("review_error", {"error": "internal_error"})
        finally:
            self._release(run)
            await self._close_providers(run)

    async def _side(
        self,
        run: ReviewRun,
        side: str,
        reviewer,
        code: str,
        cost_fn: CostFn,
        attempt_timeout: float | None,
        timeout: float,
    ) -> None:
        verdict = await guarded(
            reviewer.review(code, run.questions, timeout=attempt_timeout), timeout
        )
        result = ReviewSide(
            provider=run.sides[side],
            verdict=verdict,
            cost_usd=cost_fn(verdict.usage) if verdict.error is None else None,
        )
        run.results[side] = result
        run.publish("side_done", {"side": side, **result.model_dump()})


manager = ReviewManager(review_slots)
