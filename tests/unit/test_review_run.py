"""A review is a two-lane race: each side publishes its answer the moment it
lands, so the UI can show Jev finishing while the LLM is still thinking."""

import asyncio
import json

import pytest

from app.deps import ReviewSlots
from app.review_run import ReviewManager, ReviewRun
from tests.conftest import FakeClassifier, flat_cost

QUESTIONS = ["Is there a secret?", "Is there a bad call?"]
MARKER = "UNIQUE-PASTED-CODE-MARKER"
CODE = f"password = 'hunter2'  # TODO {MARKER}\n"


class Closable:
    def __init__(self):
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


async def start(
    manager: ReviewManager,
    jev: FakeClassifier | None = None,
    llm: FakeClassifier | None = None,
    attempt_timeout: float | None = None,
    timeout: float = 5.0,
    providers=None,
):
    jev = jev or FakeClassifier("jev")
    llm = llm or FakeClassifier("llm", probabilistic=False)
    run = await manager.start(
        code=CODE,
        questions=QUESTIONS,
        reviewers={"jev": jev, "llm": llm},
        cost_fns={"jev": flat_cost, "llm": flat_cost},
        sides_meta={"jev": "jev-latest", "llm": "fake-llm"},
        attempt_timeout=attempt_timeout,
        timeout=timeout,
        providers=providers,
    )
    return run, jev, llm


async def test_providers_are_closed_exactly_once_however_the_run_ends():
    m = ReviewManager(ReviewSlots(limit=3))
    finished = Closable()
    run, _, _ = await start(m, providers=finished)
    await wait_done(run)
    await m.cancel(run.id)
    assert finished.closed == 1

    cancelled = Closable()
    gate = asyncio.Event()
    run, _, _ = await start(m, llm=FakeClassifier("llm", gate=gate, probabilistic=False), providers=cancelled)
    await asyncio.sleep(0.02)
    await m.cancel(run.id)
    assert cancelled.closed == 1

    never_ran = Closable()
    run, _, _ = await start(m, llm=FakeClassifier("llm", gate=asyncio.Event()), providers=never_ran)
    await m.cancel(run.id)
    assert never_ran.closed == 1


async def wait_done(run: ReviewRun) -> None:
    assert run.task is not None
    await run.task


def kinds(run: ReviewRun) -> list[str]:
    return [e[1] for e in run._buffer]


def side_events(run: ReviewRun) -> dict[str, dict]:
    return {e[2]["side"]: e[2] for e in run._buffer if e[1] == "side_done"}


async def test_publishes_start_one_side_done_per_side_and_review_done():
    m = ReviewManager(ReviewSlots(limit=2))
    run, jev, _ = await start(m)
    await wait_done(run)
    assert run.status == "done"
    events = kinds(run)
    assert events[0] == "start" and events[-1] == "review_done"
    assert events.count("side_done") == 2
    first = run._buffer[0][2]
    assert first["review_id"] == run.id
    assert first["questions"] == QUESTIONS
    assert first["sides"] == {"jev": "jev-latest", "llm": "fake-llm"}
    sides = side_events(run)
    assert [a["id"] for a in sides["jev"]["verdict"]["answers"]] == ["q1", "q2"]
    assert sides["jev"]["verdict"]["answers"][0] == {"id": "q1", "p": 0.8, "yes": True}
    assert sides["jev"]["cost_usd"] == 0.001
    assert sides["llm"]["provider"] == "fake-llm"
    assert all(a["p"] is None for a in sides["llm"]["verdict"]["answers"])
    assert run._buffer[-1][2]["elapsed_ms"] >= 0
    assert jev.review_calls[0][:2] == (CODE, QUESTIONS)


async def test_each_side_is_published_the_moment_it_finishes():
    m = ReviewManager(ReviewSlots(limit=2))
    gate = asyncio.Event()
    run, _, _ = await start(m, llm=FakeClassifier("llm", gate=gate, probabilistic=False))
    for _ in range(100):
        await asyncio.sleep(0.005)
        if "jev" in side_events(run):
            break
    assert set(side_events(run)) == {"jev"}  # the LLM is still gated
    assert run.status == "running"
    gate.set()
    await wait_done(run)
    assert set(side_events(run)) == {"jev", "llm"}


async def test_slot_is_held_while_running_and_released_after():
    slots = ReviewSlots(limit=1)
    m = ReviewManager(slots)
    gate = asyncio.Event()
    run, _, _ = await start(m, llm=FakeClassifier("llm", gate=gate, probabilistic=False))
    assert slots.in_flight == 1
    refused, _, _ = await start(m)
    assert refused is None  # no queueing: the caller is told to try later
    gate.set()
    await wait_done(run)
    assert slots.in_flight == 0
    again, _, _ = await start(m)
    assert again is not None
    await wait_done(again)


async def test_cancel_stops_in_flight_calls_and_frees_the_slot():
    slots = ReviewSlots(limit=1)
    m = ReviewManager(slots)
    gate = asyncio.Event()
    stuck = FakeClassifier("llm", gate=gate, probabilistic=False)
    run, _, _ = await start(m, llm=stuck)
    await asyncio.sleep(0.02)
    assert await m.cancel(run.id) is True
    assert run.status == "cancelled"
    assert stuck.cancelled >= 1
    assert kinds(run)[-1] == "cancelled"
    assert slots.in_flight == 0


async def test_cancel_before_the_task_runs_still_terminates_and_frees_the_slot():
    slots = ReviewSlots(limit=1)
    m = ReviewManager(slots)
    run, _, _ = await start(m, llm=FakeClassifier("llm", gate=asyncio.Event()))
    await m.cancel(run.id)  # no await in between: the task may never have started
    assert run.status == "cancelled" and run.is_finished
    assert kinds(run)[-1] == "cancelled"
    assert slots.in_flight == 0


async def test_side_timeout_becomes_a_timeout_verdict_not_a_failed_run():
    m = ReviewManager(ReviewSlots(limit=2))
    never = FakeClassifier("llm", gate=asyncio.Event(), probabilistic=False)
    run, _, _ = await start(m, llm=never, timeout=0.05)
    await wait_done(run)
    assert run.status == "done"
    sides = side_events(run)
    assert sides["llm"]["verdict"]["error"] == "timeout"
    assert sides["llm"]["cost_usd"] is None
    assert sides["jev"]["verdict"]["error"] is None


async def test_side_exception_becomes_upstream_error():
    m = ReviewManager(ReviewSlots(limit=2))
    run, _, _ = await start(m, llm=FakeClassifier("llm", review_raises=True))
    await wait_done(run)
    assert run.status == "done"
    assert side_events(run)["llm"]["verdict"]["error"] == "upstream_error"


async def test_attempt_timeout_is_passed_to_each_reviewer():
    m = ReviewManager(ReviewSlots(limit=2))
    run, jev, llm = await start(m, attempt_timeout=25.0)
    await wait_done(run)
    assert jev.review_calls[0][2] == 25.0
    assert llm.review_calls[0][2] == 25.0


async def test_frames_never_contain_the_code():
    m = ReviewManager(ReviewSlots(limit=2))
    run, _, _ = await start(m)
    await wait_done(run)
    assert MARKER not in json.dumps([e[2] for e in run._buffer], default=str)


async def test_ids_are_unguessable_and_finished_runs_expire(monkeypatch):
    m = ReviewManager(ReviewSlots(limit=2))
    run, _, _ = await start(m)
    await wait_done(run)
    assert len(run.id) >= 20 and not run.id.isdigit()
    assert m.get(run.id) is not None
    monkeypatch.setattr(run, "finished_at", run.finished_at - 16 * 60)
    assert m.get(run.id) is None


async def test_each_run_releases_exactly_one_slot():
    """release() clamps at zero, so a double release would hide inside a
    single-run test. Two runs make it visible."""
    slots = ReviewSlots(limit=2)
    m = ReviewManager(slots)
    gate = asyncio.Event()
    first, _, _ = await start(m, llm=FakeClassifier("llm", gate=gate, probabilistic=False))
    second, _, _ = await start(m, llm=FakeClassifier("llm", gate=gate, probabilistic=False))
    assert slots.in_flight == 2
    await asyncio.sleep(0.02)
    await m.cancel(first.id)
    assert slots.in_flight == 1  # a double release would read 0 here
    await m.cancel(second.id)
    assert slots.in_flight == 0
    await m.cancel(first.id)  # cancelling an already-finished run releases nothing
    assert slots.in_flight == 0


async def test_unknown_id_cannot_be_cancelled():
    m = ReviewManager(ReviewSlots(limit=2))
    assert await m.cancel("nope") is False
