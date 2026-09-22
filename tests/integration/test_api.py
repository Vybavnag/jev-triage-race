"""Integration tests: real FastAPI app over ASGITransport, fake classifiers.

httpx >= 0.28 removed the `app=` shortcut — ASGITransport is required.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.deps import configure_limits, race_bucket, review_bucket
from app.main import LARGE_BODY_BYTES, MAX_BODY_BYTES, create_app
from app.race import manager
from app.schemas import MAX_CODE_CHARS, MAX_TICKET_CHARS
from tests.conftest import KEYS, SENTINEL_ANTHROPIC, SENTINEL_KEY, FakeClassifier, FakeProviders


def build_app(settings: Settings, tickets, jev=None, llm=None, static_dir=None):
    """The real app with its provider factory swapped for fakes. The factory
    seam is what the request-time key handling feeds, so header validation
    still runs in every test."""
    app = create_app(static_dir=static_dir)
    app.dependency_overrides = {}
    app.router.on_startup.clear()
    app.router.on_shutdown.clear()
    app.router.lifespan_context = _noop_lifespan(settings, tickets, jev, llm)
    return app


def _noop_lifespan(settings, tickets, jev, llm):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings
        app.state.tickets = tickets
        app.state.built = []
        configure_limits(settings)

        def factory(s, keys):
            providers = FakeProviders(s, keys, jev, llm)
            app.state.built.append(providers)
            return providers

        app.state.provider_factory = factory
        yield

    return lifespan


def make_client(app) -> httpx.AsyncClient:
    """A visitor who has entered both keys."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=KEYS
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(race_max_items=5, _env_file=None)


@pytest.fixture
async def client(settings, tickets5):
    app = build_app(settings, tickets5)
    async with make_client(app) as c:
        async with app.router.lifespan_context(app):
            yield c


@pytest.fixture(autouse=True)
def _clean_manager():
    manager._runs.clear()
    yield
    manager._runs.clear()


ANTH = {"kind": "anthropic", "model_id": "claude-sonnet-5"}
CODE = "password = 'hunter2'\n# TODO: rotate this\n"
REVIEW = {"code": CODE, "questions": ["Is there a secret?", "Is there a bad call?"], "opponent": ANTH}


RACE_TERMINAL = {"race_done", "cancelled", "race_error"}
REVIEW_TERMINAL = {"review_done", "cancelled", "review_error"}


async def sse_events(client, path: str, terminal: set[str]) -> list[tuple[str, dict]]:
    """Every (event, data) pair up to and including the terminal event."""
    events: list[tuple[str, dict]] = []
    async with client.stream("GET", path) as resp:
        assert resp.status_code == 200, resp.status_code
        event = None
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ").strip()
            elif line.startswith("data: ") and event is not None:
                events.append((event, json.loads(line.removeprefix("data: "))))
                if event in terminal:
                    break
    return events


async def stream_events(client, race_id: str) -> list[tuple[str, dict]]:
    return await sse_events(client, f"/api/race/{race_id}/stream", RACE_TERMINAL)


async def review_events(client, review_id: str) -> list[tuple[str, dict]]:
    return await sse_events(client, f"/api/review/{review_id}/stream", REVIEW_TERMINAL)


async def run_review(client, body: dict = None, headers: dict | None = None):
    """Start a review and wait for its terminal event: (response, events)."""
    r = await client.post("/api/review", json=body or REVIEW, headers=headers or {})
    if r.status_code != 201:
        return r, []
    return r, await review_events(client, r.json()["review_id"])


# ── config ───────────────────────────────────────────────────────────────────


async def test_config_exposes_models_and_no_secrets(client):
    r = await client.get("/api/config")
    assert r.status_code == 200
    body = r.text
    assert SENTINEL_KEY not in body
    data = r.json()
    assert data["dataset_size"] == 5
    assert data["race_max_items"] == 5
    assert any(m["id"] == "claude-sonnet-5" for m in data["anthropic_models"])
    assert data["token_required"] is False
    assert "race_token" not in body and "api_key" not in body
    assert "playground" not in body
    assert SENTINEL_ANTHROPIC not in body


async def test_config_exposes_review_and_own_ticket_limits(tickets5):
    settings = Settings(
        race_max_items=25, _env_file=None
    )
    app = build_app(settings, tickets5)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            data = (await c.get("/api/config")).json()
    assert data["race_max_items"] == 5  # bundled: clamped to the dataset
    assert data["max_own_tickets"] == 25  # pasted: the operator's cap alone
    assert data["max_ticket_chars"] == MAX_TICKET_CHARS
    review = data["review"]
    assert review["max_questions"] == 10
    assert review["max_question_chars"] == 200
    assert review["max_code_chars"] == MAX_CODE_CHARS
    assert 1 <= len(review["default_questions"]) <= 10
    assert all(3 <= len(q) <= 200 for q in review["default_questions"])


async def test_health(client):
    assert (await client.get("/api/health")).json() == {"ok": True}


# ── race lifecycle ───────────────────────────────────────────────────────────


async def test_race_start_stream_and_totals(client):
    r = await client.post("/api/race", json={"opponent": ANTH, "ticket_count": 3})
    assert r.status_code == 201
    race_id = r.json()["race_id"]
    assert len(race_id) >= 20

    events = []
    async with client.stream("GET", f"/api/race/{race_id}/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                events.append(line.removeprefix("event: ").strip())
            if events and events[-1] == "race_done":
                break

    assert events[0] == "start"
    assert events.count("done") == 2
    assert events[-1] == "race_done"
    assert sum(1 for e in events if e in ("result", "error")) == 6  # 3 tickets x 2 sides


async def test_bundled_race_sends_text_and_expected_labels_up_front(client):
    r = await client.post("/api/race", json={"opponent": ANTH, "ticket_count": 2})
    events = await stream_events(client, r.json()["race_id"])
    start = events[0][1]
    assert start["labeled"] is True
    assert len(start["tickets"]) == 2
    assert start["tickets"][0]["text"] == "site is down, urgent!"
    assert start["tickets"][0]["expected"] == {"urgent": True, "team": "technical", "frustration": 4}
    assert set(events[-1][1]["winner"]) == {"speed", "cost"}


# ── race: own tickets ────────────────────────────────────────────────────────


async def test_race_own_tickets_stream(client):
    tickets = ["Server is down NOW", "please resend the invoice", "can you add a seat?"]
    r = await client.post("/api/race", json={"opponent": ANTH, "tickets": tickets})
    assert r.status_code == 201
    assert r.json()["total"] == 3
    events = await stream_events(client, r.json()["race_id"])
    start = events[0][1]
    assert events[0][0] == "start" and start["labeled"] is False
    # Ids only: the client already has its own text, and frames never echo it.
    assert start["tickets"] == [{"id": f"p{i}", "text": None, "expected": None} for i in (1, 2, 3)]
    results = [d for e, d in events if e == "result"]
    assert len(results) == 6
    assert {d["ticket_id"] for d in results} == {"p1", "p2", "p3"}
    assert all("correct" not in d for d in results)
    kind, final = events[-1]
    assert kind == "race_done"
    assert set(final["winner"]) == {"speed", "cost"}
    assert final["totals"]["jev"]["scored"] == 3


async def test_race_own_tickets_are_what_gets_classified(settings, tickets5):
    jev = FakeClassifier("jev")
    app = build_app(settings, tickets5, jev=jev)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            r = await c.post("/api/race", json={"opponent": ANTH, "tickets": ["alpha one", "beta two"]})
            await stream_events(c, r.json()["race_id"])
    assert sorted(jev.calls) == ["alpha one", "beta two"]


async def test_race_own_ticket_cap_is_race_max_items_not_dataset_size(tickets5):
    settings = Settings(
        race_max_items=25, _env_file=None
    )
    app = build_app(settings, tickets5)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            ok = await c.post("/api/race", json={"opponent": ANTH, "tickets": ["t"] * 10})
            assert ok.status_code == 201  # 10 > dataset size 5, but under the cap
            await c.delete(f"/api/race/{ok.json()['race_id']}")
            over = await c.post("/api/race", json={"opponent": ANTH, "tickets": ["t"] * 26})
            assert over.status_code == 422
            assert "cap" in over.json()["detail"]


@pytest.mark.parametrize("body", [{}, {"ticket_count": 1, "tickets": ["a"]}])
async def test_race_requires_exactly_one_source(client, body):
    r = await client.post("/api/race", json={"opponent": ANTH, **body})
    assert r.status_code == 422


async def test_race_oversized_own_ticket_rejected_before_calls_and_tokens(settings, tickets5):
    jev = FakeClassifier("jev")
    app = build_app(settings, tickets5, jev=jev)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            r = await c.post(
                "/api/race", json={"opponent": ANTH, "tickets": ["x" * (MAX_TICKET_CHARS + 1)]}
            )
    assert r.status_code == 422
    assert jev.calls == []
    assert race_bucket._state == {}


async def test_race_too_many_own_tickets_spends_no_token(client):
    r = await client.post("/api/race", json={"opponent": ANTH, "tickets": ["t"] * 6})
    assert r.status_code == 422
    assert race_bucket._state == {}


async def test_race_own_tickets_never_appear_in_frames(client):
    marker = "UNIQUE-PASTED-MARKER-XYZ"
    r = await client.post("/api/race", json={"opponent": ANTH, "tickets": [f"help {marker}", "other"]})
    race_id = r.json()["race_id"]
    chunks = []
    async with client.stream("GET", f"/api/race/{race_id}/stream") as resp:
        async for chunk in resp.aiter_text():
            chunks.append(chunk)
            if "race_done" in chunk:
                break
    assert marker not in "".join(chunks)


# ── review ───────────────────────────────────────────────────────────────────


@pytest.fixture
async def review_app(settings, tickets5):
    jev = FakeClassifier("jev", probabilistic=True)
    llm = FakeClassifier("llm", probabilistic=False)
    app = build_app(settings, tickets5, jev=jev, llm=llm)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            yield c, jev, llm


def sides_of(events: list[tuple[str, dict]]) -> dict[str, dict]:
    return {d["side"]: d for e, d in events if e == "side_done"}


async def test_review_streams_each_side_as_it_finishes(review_app):
    c, jev, llm = review_app
    r, events = await run_review(c)
    assert r.status_code == 201
    assert len(r.json()["review_id"]) >= 20
    kinds = [e for e, _ in events]
    assert kinds[0] == "start" and kinds[-1] == "review_done"
    assert kinds.count("side_done") == 2
    first = events[0][1]
    assert first["questions"] == REVIEW["questions"]
    assert first["sides"] == {"jev": "jev-latest", "llm": "claude-sonnet-5"}
    sides = sides_of(events)
    jev_answers = sides["jev"]["verdict"]["answers"]
    assert [a["id"] for a in jev_answers] == ["q1", "q2"]
    assert jev_answers[0] == {"id": "q1", "p": 0.8, "yes": True}
    assert all(a["p"] is None and a["yes"] is True for a in sides["llm"]["verdict"]["answers"])
    assert sides["jev"]["cost_usd"] is not None
    assert sides["llm"]["provider"] == "claude-sonnet-5"
    assert jev.review_calls[0][:2] == (CODE, REVIEW["questions"])
    assert jev.review_calls[0][2] == 25.0  # the per-attempt timeout is passed through


async def test_review_oversized_code_rejected_before_calls_and_tokens(review_app):
    c, jev, _ = review_app
    r = await c.post("/api/review", json={**REVIEW, "code": "x" * (MAX_CODE_CHARS + 1)})
    assert r.status_code == 422
    assert jev.review_calls == []
    assert review_bucket._state == {}


async def test_review_duplicate_questions_422(review_app):
    c, _, _ = review_app
    r = await c.post("/api/review", json={**REVIEW, "questions": ["Is it safe?", " is IT safe? "]})
    assert r.status_code == 422
    assert "duplicate" in r.text


async def test_review_eleven_questions_422(review_app):
    c, _, _ = review_app
    r = await c.post("/api/review", json={**REVIEW, "questions": [f"Is problem {i} here?" for i in range(11)]})
    assert r.status_code == 422


async def test_review_side_timeout_is_reported_not_fatal(tickets5):
    settings = Settings(
        race_max_items=5,
        review_timeout=0.05,
        _env_file=None,
    )
    llm = FakeClassifier("llm", gate=asyncio.Event(), probabilistic=False)  # never answers
    app = build_app(settings, tickets5, llm=llm)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            r, events = await run_review(c)
    assert r.status_code == 201
    assert events[-1][0] == "review_done"
    sides = sides_of(events)
    assert sides["llm"]["verdict"]["error"] == "timeout"
    assert sides["llm"]["cost_usd"] is None
    assert sides["jev"]["verdict"]["error"] is None


async def test_review_side_exception_is_upstream_error_not_fatal(settings, tickets5):
    llm = FakeClassifier("llm", review_raises=True)
    app = build_app(settings, tickets5, llm=llm)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            r, events = await run_review(c)
    assert r.status_code == 201
    assert events[-1][0] == "review_done"
    assert sides_of(events)["llm"]["verdict"]["error"] == "upstream_error"
    assert sides_of(events)["jev"]["verdict"]["answers"]


async def test_review_rejects_unknown_model_and_unconfigured_opponent(review_app):
    c, _, _ = review_app
    bad_model = await c.post(
        "/api/review", json={**REVIEW, "opponent": {"kind": "anthropic", "model_id": "gpt-4"}}
    )
    assert bad_model.status_code == 422
    no_openai = await c.post(
        "/api/review", json={**REVIEW, "opponent": {"kind": "openai_compat", "model_id": "gpt-x"}}
    )
    assert no_openai.status_code == 503


async def test_review_token_gate(tickets5):
    settings = Settings(
        race_token="s3cret-token-value",
        race_max_items=5,
        _env_file=None,
    )
    app = build_app(settings, tickets5)
    auth = {"Authorization": "Bearer s3cret-token-value"}
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            assert (await c.post("/api/review", json=REVIEW)).status_code == 401
            wrong = await c.post("/api/review", json=REVIEW, headers={"Authorization": "Bearer nope"})
            assert wrong.status_code == 401
            ok, events = await run_review(c, headers=auth)
            assert ok.status_code == 201 and events[-1][0] == "review_done"
            # Cancels are gated too; the stream itself needs only the id.
            assert (await c.delete(f"/api/review/{ok.json()['review_id']}")).status_code == 401


async def test_review_rate_limit_bursts_five_then_429(review_app):
    c, _, _ = review_app
    codes = [(await run_review(c))[0].status_code for _ in range(review_bucket.capacity + 1)]
    assert codes[: review_bucket.capacity] == [201] * review_bucket.capacity
    assert codes[-1] == 429


async def test_review_422_spends_no_token(review_app):
    c, _, _ = review_app
    assert (await c.post("/api/review", json={**REVIEW, "code": ""})).status_code == 422
    codes = [(await run_review(c))[0].status_code for _ in range(review_bucket.capacity)]
    assert codes == [201] * review_bucket.capacity


async def test_review_and_race_buckets_are_independent(review_app):
    c, _, _ = review_app
    for _ in range(review_bucket.capacity):
        assert (await run_review(c))[0].status_code == 201
    assert (await c.post("/api/review", json=REVIEW)).status_code == 429
    race = await c.post("/api/race", json={"opponent": ANTH, "ticket_count": 1})
    assert race.status_code == 201
    await c.delete(f"/api/race/{race.json()['race_id']}")


async def test_review_busy_when_two_reviews_are_in_flight(settings, tickets5):
    gate = asyncio.Event()
    jev = FakeClassifier("jev", gate=gate)
    llm = FakeClassifier("llm", gate=gate, probabilistic=False)
    app = build_app(settings, tickets5, jev=jev, llm=llm)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            first = await c.post("/api/review", json=REVIEW)
            second = await c.post("/api/review", json=REVIEW)
            assert first.status_code == 201 and second.status_code == 201
            third = await c.post("/api/review", json=REVIEW)
            assert third.status_code == 503
            assert "busy" in third.json()["detail"]
            # Only the two admitted reviews spent a rate-limit token.
            (tokens, _ts), = review_bucket._state.values()
            assert tokens == pytest.approx(review_bucket.capacity - 2, abs=0.01)
            gate.set()
            for r in (first, second):
                events = await review_events(c, r.json()["review_id"])
                assert events[-1][0] == "review_done"
            fourth = await c.post("/api/review", json=REVIEW)
            assert fourth.status_code == 201  # slots were released


async def test_review_busy_applies_in_token_mode(tickets5):
    settings = Settings(
        race_token="s3cret-token-value",
        race_max_items=5,
        _env_file=None,
    )
    gate = asyncio.Event()
    jev = FakeClassifier("jev", gate=gate)
    llm = FakeClassifier("llm", gate=gate, probabilistic=False)
    app = build_app(settings, tickets5, jev=jev, llm=llm)
    auth = {"Authorization": "Bearer s3cret-token-value"}
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            first = await c.post("/api/review", json=REVIEW, headers=auth)
            second = await c.post("/api/review", json=REVIEW, headers=auth)
            third = await c.post("/api/review", json=REVIEW, headers=auth)
            assert (first.status_code, second.status_code, third.status_code) == (201, 201, 503)
            gate.set()
            for r in (first, second):
                await review_events(c, r.json()["review_id"])


async def test_review_slot_is_released_after_a_side_fails(settings, tickets5):
    llm = FakeClassifier("llm", review_raises=True)
    app = build_app(settings, tickets5, llm=llm)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            # More sequential reviews than slots: a leaked slot would 503 here.
            for _ in range(3):
                assert (await run_review(c))[0].status_code == 201


async def test_review_cancel_stops_it_and_frees_the_slot(settings, tickets5):
    gate = asyncio.Event()
    llm = FakeClassifier("llm", gate=gate, probabilistic=False)
    app = build_app(settings, tickets5, llm=llm)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            started = await c.post("/api/review", json=REVIEW)
            review_id = started.json()["review_id"]
            await asyncio.sleep(0.02)
            assert (await c.delete(f"/api/review/{review_id}")).status_code == 204
            events = await review_events(c, review_id)
            assert events[-1][0] == "cancelled"
            assert llm.cancelled >= 1
            again = await c.post("/api/review", json=REVIEW)
            assert again.status_code == 201
            gate.set()
            await review_events(c, again.json()["review_id"])
            assert (await c.delete("/api/review/doesnotexist")).status_code == 404
            assert (await c.get("/api/review/doesnotexist/stream")).status_code == 404


async def test_review_stream_replays_from_last_event_id(review_app):
    c, _, _ = review_app
    r, _ = await run_review(c)
    review_id = r.json()["review_id"]
    async with c.stream(
        "GET", f"/api/review/{review_id}/stream", headers={"Last-Event-ID": "1"}
    ) as resp:
        seqs, kinds = [], []
        async for line in resp.aiter_lines():
            if line.startswith("id: "):
                seqs.append(int(line.removeprefix("id: ")))
            if line.startswith("event: "):
                kinds.append(line.removeprefix("event: ").strip())
            if kinds and kinds[-1] == "review_done":
                break
    assert min(seqs) == 2
    assert "gap" not in kinds and kinds[-1] == "review_done"


async def test_review_stream_never_echoes_pasted_code(review_app):
    c, _, _ = review_app
    pasted = "sk-PASTED-SECRET-abcdefghijklmnopqrstuvwxyz0123"
    r = await c.post("/api/review", json={**REVIEW, "code": f"key = '{pasted}'\n"})
    assert r.status_code == 201
    chunks = []
    async with c.stream("GET", f"/api/review/{r.json()['review_id']}/stream") as resp:
        async for chunk in resp.aiter_text():
            chunks.append(chunk)
            if "review_done" in chunk:
                break
    assert pasted not in "".join(chunks)


# ── transport guards ─────────────────────────────────────────────────────────


async def test_body_cap_is_per_route(client):
    headers = {"content-type": "application/json"}
    over = await client.post("/api/review", content=b"x" * (LARGE_BODY_BYTES + 1), headers=headers)
    assert over.status_code == 413
    # A large-but-legal body reaches validation (422), proving the cap is lifted here.
    big_code = json.dumps({**REVIEW, "code": "x" * (MAX_CODE_CHARS + 1) * 5})
    assert MAX_BODY_BYTES < len(big_code) < LARGE_BODY_BYTES
    under = await client.post("/api/review", content=big_code, headers=headers)
    assert under.status_code == 422
    # Everything else keeps the small cap, even a route that has no POST handler.
    other = await client.post("/api/config", content=b"x" * (MAX_BODY_BYTES + 1), headers=headers)
    assert other.status_code == 413


async def test_post_without_content_length_is_411(client):
    async def chunks():
        yield json.dumps(REVIEW).encode()

    r = await client.post(
        "/api/review", content=chunks(), headers={"content-type": "application/json"}
    )
    assert r.status_code == 411


async def test_422_body_omits_the_offending_input(client):
    marker = "SECRET-IN-INPUT-42"
    r = await client.post("/api/review", json={**REVIEW, "code": marker + "x" * MAX_CODE_CHARS})
    assert r.status_code == 422
    assert marker not in r.text
    assert '"input"' not in r.text
    assert r.json()["detail"][0]["loc"] == ["body", "code"]


async def test_lone_surrogate_is_422_not_500(client):
    raw = (
        b'{"code": "a\\ud800b", "questions": ["Is it safe?"], '
        b'"opponent": {"kind": "anthropic", "model_id": "claude-sonnet-5"}}'
    )
    r = await client.post("/api/review", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 422


async def test_invalid_body_422_precedes_401_in_token_mode(tickets5):
    """Body validation runs before the handler's token check. That lets an
    unauthenticated caller probe validation, which is accepted: it reveals
    only the public schema and spends nothing."""
    settings = Settings(
        race_token="s3cret-token-value",
        race_max_items=5,
        _env_file=None,
    )
    app = build_app(settings, tickets5)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            assert (await c.post("/api/review", json={**REVIEW, "code": ""})).status_code == 422


async def test_playground_route_removed(settings, tickets5):
    # Without a static mount the API answers for unknown paths itself; with
    # one, StaticFiles would answer 405 and hide whether the route exists.
    app = build_app(settings, tickets5, static_dir=Path("/nonexistent"))
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            r = await c.post("/api/playground", json={"text": "hi", "opponent": ANTH})
    assert r.status_code == 404


async def test_result_events_carry_confidence_for_the_routing_gate(client):
    """The gate can only exist if each result reports the confidence it was
    decided at, so the field has to reach the client per ticket."""
    r = await client.post("/api/race", json={"opponent": ANTH, "ticket_count": 2})
    race_id = r.json()["race_id"]
    verdicts = []
    async with client.stream("GET", f"/api/race/{race_id}/stream") as resp:
        event = None
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ").strip()
            elif line.startswith("data: "):
                payload = json.loads(line.removeprefix("data: "))
                if event == "result":
                    verdicts.append(payload["verdict"])
                elif event == "race_done":
                    break
    assert verdicts
    assert all("team_confidence" in v for v in verdicts)


async def test_race_cap_enforced(client):
    r = await client.post("/api/race", json={"opponent": ANTH, "ticket_count": 99})
    assert r.status_code == 422
    assert "cap" in r.json()["detail"]


async def test_race_rejects_unknown_model(client):
    r = await client.post(
        "/api/race", json={"opponent": {"kind": "anthropic", "model_id": "gpt-4"}, "ticket_count": 1}
    )
    assert r.status_code == 422


async def test_race_rejects_extra_fields(client):
    r = await client.post(
        "/api/race",
        json={
            "opponent": {"kind": "anthropic", "model_id": "claude-sonnet-5", "base_url": "http://169.254.169.254"},
            "ticket_count": 1,
        },
    )
    assert r.status_code == 422


async def test_openai_compat_rejected_when_unconfigured(client):
    r = await client.post(
        "/api/race",
        json={"opponent": {"kind": "openai_compat", "model_id": "gpt-x"}, "ticket_count": 1},
    )
    assert r.status_code == 503


async def test_unknown_race_id_404(client):
    assert (await client.get("/api/race/doesnotexist/stream")).status_code == 404
    assert (await client.delete("/api/race/doesnotexist")).status_code == 404


async def test_second_race_conflicts_then_cancel_frees(settings, tickets5):
    gate = asyncio.Event()
    app = build_app(settings, tickets5, llm=FakeClassifier("llm", gate=gate))
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            first = await c.post("/api/race", json={"opponent": ANTH, "ticket_count": 2})
            assert first.status_code == 201
            await asyncio.sleep(0.05)
            second = await c.post("/api/race", json={"opponent": ANTH, "ticket_count": 2})
            assert second.status_code == 409
            # A rejected duplicate must not have spent a rate-limit token.
            assert race_bucket._state  # first race did consume one
            assert (await c.delete(f"/api/race/{first.json()['race_id']}")).status_code == 204
            # Isolate the lock-release assertion from the per-IP bucket, which
            # legitimately blocks back-to-back races (covered by its own test).
            race_bucket.reset()
            third = await c.post("/api/race", json={"opponent": ANTH, "ticket_count": 2})
            assert third.status_code == 201
            gate.set()


async def test_last_event_id_replay(client):
    r = await client.post("/api/race", json={"opponent": ANTH, "ticket_count": 2})
    race_id = r.json()["race_id"]
    async with client.stream("GET", f"/api/race/{race_id}/stream") as resp:
        async for line in resp.aiter_lines():
            if line.startswith("event: race_done"):
                break
    # Reconnect from the very beginning: the buffer still holds everything.
    async with client.stream(
        "GET", f"/api/race/{race_id}/stream", headers={"Last-Event-ID": "1"}
    ) as resp:
        seqs, events = [], []
        async for line in resp.aiter_lines():
            if line.startswith("id: "):
                seqs.append(int(line.removeprefix("id: ")))
            if line.startswith("event: "):
                events.append(line.removeprefix("event: ").strip())
            if events and events[-1] == "race_done":
                break
    assert min(seqs) == 2  # strictly after the requested id
    assert "gap" not in events
    assert events[-1] == "race_done"


# ── security headers & secret sweep ──────────────────────────────────────────


CSP = "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"


async def test_csp_exact_on_api_health(client):
    r = await client.get("/api/health")
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_csp_exact_on_spa_index(settings, tickets5, tmp_path):
    """The SPA document carries the same exact policy. Fonts are self-hosted
    for this reason: any asset that needed another origin would break here."""
    (tmp_path / "index.html").write_text("<!doctype html><title>race</title>")
    app = build_app(settings, tickets5, static_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            r = await c.get("/")
            assert r.status_code == 200
            assert r.headers["content-security-policy"] == CSP


async def test_no_key_in_any_response_including_sse(client):
    bodies = []
    bodies.append((await client.get("/api/config")).text)
    bodies.append((await client.post("/api/race", json={"opponent": ANTH, "ticket_count": 99})).text)
    started = await client.post("/api/review", json=REVIEW)
    bodies.append(started.text)
    async with client.stream("GET", f"/api/review/{started.json()['review_id']}/stream") as resp:
        async for chunk in resp.aiter_text():
            bodies.append(chunk)
            if "review_done" in chunk:
                break
    for body in ({"ticket_count": 2}, {"tickets": ["one", "two"]}):
        r = await client.post("/api/race", json={"opponent": ANTH, **body})
        race_id = r.json()["race_id"]
        async with client.stream("GET", f"/api/race/{race_id}/stream") as resp:
            async for chunk in resp.aiter_text():
                bodies.append(chunk)
                if "race_done" in chunk:
                    break
    for b in bodies:
        assert SENTINEL_KEY not in b
        assert SENTINEL_ANTHROPIC not in b


async def test_boots_with_no_keys_and_asks_the_visitor_for_them(tickets5):
    """Nothing about providers lives in the environment. The app comes up
    without any key, lists every opponent, and refuses work that does not
    carry the visitor's keys before touching a provider or a bucket."""
    settings = Settings(_env_file=None)
    app = build_app(settings, tickets5)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:  # deliberately no key headers
            assert (await c.get("/api/health")).json() == {"ok": True}
            cfg = await c.get("/api/config")
            assert cfg.status_code == 200
            assert [m["id"] for m in cfg.json()["anthropic_models"]]
            race = await c.post("/api/race", json={"opponent": ANTH, "ticket_count": 1})
            assert race.status_code == 400
            assert "X-TypeSafe-Key" in race.json()["detail"]
            review = await c.post("/api/review", json=REVIEW)
            assert review.status_code == 400
            assert "X-TypeSafe-Key" in review.json()["detail"]
            only_ts = await c.post(
                "/api/race", json={"opponent": ANTH, "ticket_count": 1}, headers={"X-TypeSafe-Key": SENTINEL_KEY}
            )
            assert only_ts.status_code == 400
            assert "X-Anthropic-Key" in only_ts.json()["detail"]
    assert race_bucket._state == {} and review_bucket._state == {}


async def test_anthropic_key_is_not_needed_for_an_openai_compatible_opponent(tickets5):
    settings = Settings(
        openai_compat_base_url="https://llm.example.com/v1",
        openai_compat_model="gpt-x",
        race_max_items=5,
        _env_file=None,
    )
    app = build_app(settings, tickets5)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"X-TypeSafe-Key": SENTINEL_KEY}
        ) as c:
            r = await c.post(
                "/api/race", json={"opponent": {"kind": "openai_compat", "model_id": "gpt-x"}, "ticket_count": 1}
            )
            assert r.status_code == 201
            await stream_events(c, r.json()["race_id"])


async def test_malformed_key_header_is_rejected_before_anything(client):
    r = await client.post(
        "/api/race", json={"opponent": ANTH, "ticket_count": 1}, headers={"X-TypeSafe-Key": "short"}
    )
    assert r.status_code == 400
    assert "short" not in r.text
    assert race_bucket._state == {}


async def test_providers_are_built_from_the_visitor_keys_and_closed_after_the_run(settings, tickets5):
    app = build_app(settings, tickets5)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            r = await c.post("/api/race", json={"opponent": ANTH, "ticket_count": 1})
            await stream_events(c, r.json()["race_id"])
            review, events = await run_review(c)
            assert events[-1][0] == "review_done"
    assert len(app.state.built) == 2
    for providers in app.state.built:
        assert providers.keys.typesafe == SENTINEL_KEY
        assert providers.keys.anthropic == SENTINEL_ANTHROPIC
        assert providers.closed is True


async def test_providers_are_closed_when_the_start_is_refused(tickets5):
    """resolve_opponent builds a client before the last gates run, so a 429
    or a busy 503 must still close what was built."""
    settings = Settings(race_burst=1, race_max_items=5, _env_file=None)
    gate = asyncio.Event()
    jev = FakeClassifier("jev", gate=gate)
    llm = FakeClassifier("llm", gate=gate, probabilistic=False)
    app = build_app(settings, tickets5, jev=jev, llm=llm)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            first = await c.post("/api/race", json={"opponent": ANTH, "ticket_count": 1})
            assert first.status_code == 201
            await c.delete(f"/api/race/{first.json()['race_id']}")
            limited = await c.post("/api/race", json={"opponent": ANTH, "ticket_count": 1})
            assert limited.status_code == 429
            assert app.state.built[-1].closed is True

            one = await c.post("/api/review", json=REVIEW)
            two = await c.post("/api/review", json=REVIEW)
            assert (one.status_code, two.status_code) == (201, 201)
            busy = await c.post("/api/review", json=REVIEW)
            assert busy.status_code == 503
            assert app.state.built[-1].closed is True
            gate.set()
            for r in (one, two):
                await review_events(c, r.json()["review_id"])


async def test_rate_limiting_can_be_switched_off_for_a_local_clone(tickets5):
    settings = Settings(rate_limiting=False, race_max_items=5, _env_file=None)
    app = build_app(settings, tickets5)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            codes = [(await run_review(c))[0].status_code for _ in range(8)]
    assert codes == [201] * 8


async def test_bucket_sizes_come_from_the_environment(tickets5):
    settings = Settings(race_burst=1, review_burst=2, race_max_items=5, _env_file=None)
    app = build_app(settings, tickets5)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            first = await c.post("/api/race", json={"opponent": ANTH, "ticket_count": 1})
            assert first.status_code == 201
            await c.delete(f"/api/race/{first.json()['race_id']}")
            assert (await c.post("/api/race", json={"opponent": ANTH, "ticket_count": 1})).status_code == 429
            codes = [(await run_review(c))[0].status_code for _ in range(3)]
            assert codes == [201, 201, 429]


async def test_race_token_gate(tickets5):
    settings = Settings(
        race_token="s3cret-token-value",
        race_max_items=5,
        _env_file=None,
    )
    app = build_app(settings, tickets5)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            assert (await c.post("/api/race", json={"opponent": ANTH, "ticket_count": 1})).status_code == 401
            bad = await c.post(
                "/api/race",
                json={"opponent": ANTH, "ticket_count": 1},
                headers={"Authorization": "Bearer wrong"},
            )
            assert bad.status_code == 401
            ok = await c.post(
                "/api/race",
                json={"opponent": ANTH, "ticket_count": 1},
                headers={"Authorization": "Bearer s3cret-token-value"},
            )
            assert ok.status_code == 201
            cfg = await c.get("/api/config")
            assert "s3cret-token-value" not in cfg.text
            assert cfg.json()["token_required"] is True


async def test_rate_limit_allows_a_burst_then_throttles(client):
    """A local demo runs several races back to back, so the bucket must allow
    a burst; only sustained hammering gets a 429."""
    codes = []
    for _ in range(race_bucket.capacity + 1):
        r = await client.post("/api/race", json={"opponent": ANTH, "ticket_count": 1})
        codes.append(r.status_code)
        if r.status_code == 201:
            await client.delete(f"/api/race/{r.json()['race_id']}")
    assert codes[: race_bucket.capacity] == [201] * race_bucket.capacity
    assert codes[-1] == 429


# ── static serving ───────────────────────────────────────────────────────────


async def test_root_explains_itself_when_no_frontend_is_built(settings, tickets5):
    """Running the API straight from a checkout used to answer a bare 404 at
    the root, which reads as broken. It has to say which command to run."""
    app = build_app(settings, tickets5, static_dir=Path("/nonexistent"))
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            r = await c.get("/")
            assert r.status_code == 503
            body = r.json()
            assert body["error"] == "frontend_not_built"
            assert "./dev" in body["detail"] and "npm run build" in body["detail"]
            # The API must still work while the UI is missing.
            assert (await c.get("/api/health")).json() == {"ok": True}


def test_static_dir_falls_back_to_vite_build(tmp_path, monkeypatch):
    """The container bakes the SPA into static/; a checkout only has
    web/dist. Both must be found, with static/ winning."""
    import app.main as main

    container, checkout = tmp_path / "static", tmp_path / "web" / "dist"
    checkout.mkdir(parents=True)
    (checkout / "index.html").write_text("<html></html>")
    monkeypatch.setattr(main, "STATIC_CANDIDATES", (container, checkout))
    assert main.find_static_dir() == checkout

    container.mkdir()
    (container / "index.html").write_text("<html></html>")
    assert main.find_static_dir() == container

    monkeypatch.setattr(main, "STATIC_CANDIDATES", (tmp_path / "nope",))
    assert main.find_static_dir() is None


async def test_spa_served_and_api_not_swallowed(settings, tickets5, tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>race</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    app = build_app(settings, tickets5, static_dir=tmp_path)
    async with app.router.lifespan_context(app):
        async with make_client(app) as c:
            root = await c.get("/")
            assert root.status_code == 200 and "race" in root.text
            asset = await c.get("/assets/app.js")
            assert asset.status_code == 200
            assert "javascript" in asset.headers["content-type"]
            assert (await c.get("/api/health")).json() == {"ok": True}
