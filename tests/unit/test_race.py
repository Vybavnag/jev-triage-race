import asyncio

import pytest

from app.race import BUFFER_MAX_BYTES, RaceManager, RaceRun
from tests.conftest import FakeClassifier, flat_cost


async def start_simple(manager: RaceManager, tickets, jev=None, llm=None, conc=None):
    jev = jev or FakeClassifier("jev")
    llm = llm or FakeClassifier("llm")
    run = await manager.start(
        tickets=tickets,
        classifiers={"jev": jev, "llm": llm},
        cost_fns={"jev": flat_cost, "llm": flat_cost},
        concurrency=conc or {"jev": 8, "llm": 4},
        per_ticket_timeout=5.0,
        sides_meta={"jev": "jev-latest", "llm": "fake-llm"},
    )
    return run, jev, llm


async def wait_done(run: RaceRun):
    assert run.task is not None
    await run.task


async def test_race_completes_and_publishes_summary(tickets5):
    m = RaceManager()
    run, jev, llm = await start_simple(m, tickets5)
    await wait_done(run)
    assert run.status == "done"
    events = [e for e in run._buffer]
    kinds = [e[1] for e in events]
    assert kinds[0] == "start"
    assert kinds[-1] == "race_done"
    assert kinds.count("done") == 2
    # 5 tickets x 2 sides
    assert sum(1 for k in kinds if k in ("result", "error")) == 10
    final = events[-1][2]
    assert final["totals"]["jev"]["attempted"] == 5
    assert final["winner"] is not None


async def test_partial_failure_does_not_abort(tickets5):
    m = RaceManager()
    llm = FakeClassifier("llm", fail_on={"invoice"}, fail_kind="rate_limited")
    run, _, _ = await start_simple(m, tickets5, llm=llm)
    await wait_done(run)
    assert run.status == "done"
    llm_summary = run.tallies["llm"].summary()
    assert llm_summary["errors"] == 1
    assert llm_summary["scored"] == 4
    assert llm_summary["error_kinds"] == {"rate_limited": 1}
    error_events = [e for e in run._buffer if e[1] == "error"]
    assert error_events and error_events[0][2]["kind"] == "rate_limited"


async def test_concurrency_cap_respected_per_side(tickets5):
    m = RaceManager()
    tickets = tickets5 * 4  # 20 tickets, but ids duplicate — fine for orchestrator
    jev = FakeClassifier("jev", delay=0.01)
    llm = FakeClassifier("llm", delay=0.01)
    run, _, _ = await start_simple(m, tickets, jev=jev, llm=llm, conc={"jev": 3, "llm": 2})
    await wait_done(run)
    assert jev.max_live <= 3
    assert llm.max_live <= 2


async def test_sides_run_concurrently(tickets5):
    m = RaceManager()
    gate = asyncio.Event()
    slow_llm = FakeClassifier("llm", gate=gate)
    fast_jev = FakeClassifier("jev")
    run, _, _ = await start_simple(m, tickets5, jev=fast_jev, llm=slow_llm)
    # Jev side should finish all 5 while LLM is gated
    for _ in range(100):
        await asyncio.sleep(0.01)
        if run.tallies["jev"].attempted == 5:
            break
    assert run.tallies["jev"].attempted == 5
    assert run.tallies["llm"].attempted == 0
    gate.set()
    await wait_done(run)
    assert run.status == "done"


async def test_cancel_releases_lock_and_next_race_starts(tickets5):
    m = RaceManager()
    gate = asyncio.Event()
    stuck = FakeClassifier("llm", gate=gate)
    run, _, _ = await start_simple(m, tickets5, llm=stuck)
    await asyncio.sleep(0.02)
    assert m.race_in_progress
    assert await m.cancel(run.id) is True
    assert run.status == "cancelled"
    assert stuck.cancelled >= 1  # pending provider calls actually got CancelledError
    assert not m.race_in_progress
    run2, _, _ = await start_simple(m, tickets5)
    assert run2 is not None
    await wait_done(run2)
    assert run2.status == "done"


async def test_second_race_rejected_while_running(tickets5):
    m = RaceManager()
    gate = asyncio.Event()
    run, _, _ = await start_simple(m, tickets5, llm=FakeClassifier("llm", gate=gate))
    await asyncio.sleep(0.01)
    dup = await m.start(
        tickets=tickets5,
        classifiers={"jev": FakeClassifier(), "llm": FakeClassifier()},
        cost_fns={"jev": flat_cost, "llm": flat_cost},
        concurrency={},
        per_ticket_timeout=5.0,
        sides_meta={},
    )
    assert dup is None
    gate.set()
    await wait_done(run)


async def test_second_race_rejected_with_no_scheduling_gap(tickets5):
    """Regression: the slot must be claimed synchronously in start().

    When it was only taken inside the spawned task, two requests arriving in
    the same tick both passed the check and the races queued up back to back —
    silently doubling spend instead of refusing the second.
    """
    m = RaceManager()
    first, _, _ = await start_simple(m, tickets5, llm=FakeClassifier("llm", gate=asyncio.Event()))
    second = await m.start(
        tickets=tickets5,
        classifiers={"jev": FakeClassifier(), "llm": FakeClassifier()},
        cost_fns={"jev": flat_cost, "llm": flat_cost},
        concurrency={},
        per_ticket_timeout=5.0,
        sides_meta={},
    )
    assert second is None
    assert m.race_in_progress
    await m.cancel(first.id)
    assert not m.race_in_progress
    # Cancelling before the task ever ran must still reach a terminal state,
    # or a connected stream would wait forever.
    assert first.status == "cancelled"
    assert first.is_finished
    assert [e[1] for e in first._buffer][-1] == "cancelled"


async def test_concurrent_starts_admit_exactly_one(tickets5):
    m = RaceManager()
    results = await asyncio.gather(
        start_simple(m, tickets5, llm=FakeClassifier("llm", gate=asyncio.Event())),
        start_simple(m, tickets5, llm=FakeClassifier("llm", gate=asyncio.Event())),
    )
    runs = [r[0] for r in results]
    assert sum(1 for r in runs if r is not None) == 1
    winner_run = next(r for r in runs if r is not None)
    await m.cancel(winner_run.id)


async def test_ticket_timeout_becomes_error_event(tickets5):
    m = RaceManager()
    never = FakeClassifier("llm", gate=asyncio.Event())  # never set -> hangs
    run = await m.start(
        tickets=tickets5[:1],
        classifiers={"jev": FakeClassifier("jev"), "llm": never},
        cost_fns={"jev": flat_cost, "llm": flat_cost},
        concurrency={"jev": 8, "llm": 4},
        per_ticket_timeout=0.05,
        sides_meta={},
    )
    await wait_done(run)
    assert run.status == "done"
    assert run.tallies["llm"].error_kinds == {"timeout": 1}


class TestBufferReplay:
    def make_run(self) -> RaceRun:
        return RaceRun(id="r", total=1, sides={})

    def test_replay_contiguous(self):
        run = self.make_run()
        for i in range(5):
            run.publish("result", {"i": i})
        gapped, events = run.replay_from(2)
        assert gapped is False
        assert [e[0] for e in events] == [3, 4, 5]

    def test_eviction_sets_floor_and_gap(self):
        run = self.make_run()
        big = "x" * 4096
        n = (BUFFER_MAX_BYTES // 4096) + 10
        for i in range(n):
            run.publish("result", {"i": i, "pad": big})
        assert run.floor_seq > 0
        gapped, events = run.replay_from(0)
        assert gapped is True
        assert events[0][0] == run.floor_seq + 1

    def test_publish_scrubs(self):
        run = self.make_run()
        run.publish("result", {"note": "sk-ant-abcdefghijklmnopqrstuv12"})
        assert "sk-ant" not in run._buffer[0][2]["note"]

    async def test_ids_are_unguessable(self, tickets5):
        m = RaceManager()
        run, _, _ = await start_simple(m, tickets5[:1])
        await wait_done(run)
        assert len(run.id) >= 20  # token_urlsafe(16), never a sequential int
        assert not run.id.isdigit()


async def test_ttl_purges_finished_runs(tickets5, monkeypatch):
    m = RaceManager()
    run, _, _ = await start_simple(m, tickets5[:1])
    await wait_done(run)
    assert m.get(run.id) is not None
    monkeypatch.setattr(run, "finished_at", run.finished_at - 16 * 60)
    assert m.get(run.id) is None
