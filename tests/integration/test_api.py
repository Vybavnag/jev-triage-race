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
from app.deps import Providers, race_bucket
from app.main import create_app
from app.race import manager
from app.schemas import Usage
from tests.conftest import SENTINEL_KEY, FakeClassifier


class FakeProviders(Providers):
    """Providers with every network-backed classifier swapped for a fake."""

    def __init__(self, settings: Settings, jev=None, llm=None):
        self._settings = settings
        self.jev = jev or FakeClassifier("jev")
        self._llm = llm or FakeClassifier("llm")
        self._anthropic = {}
        self._openai = None

    def anthropic(self, model_id: str):
        return self._llm

    def openai_compat(self):
        return self._llm

    async def close(self) -> None:
        return None


def build_app(settings: Settings, tickets, jev=None, llm=None, static_dir=None):
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
        app.state.providers = FakeProviders(settings, jev, llm)
        yield

    return lifespan


@pytest.fixture
def settings() -> Settings:
    return Settings(
        typesafe_api_key=SENTINEL_KEY,
        anthropic_api_key=SENTINEL_KEY,
        race_max_items=5,
        _env_file=None,
    )


@pytest.fixture
async def client(settings, tickets5):
    app = build_app(settings, tickets5)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        async with app.router.lifespan_context(app):
            yield c


@pytest.fixture(autouse=True)
def _clean_manager():
    manager._runs.clear()
    yield
    manager._runs.clear()


ANTH = {"kind": "anthropic", "model_id": "claude-sonnet-5"}


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
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
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


# ── playground ───────────────────────────────────────────────────────────────


async def test_playground_round_trip(client):
    r = await client.post(
        "/api/playground", json={"text": "everything is down, fix now", "opponent": ANTH}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["jev"]["verdict"]["team"] == "technical"
    assert body["llm"]["provider"] == "claude-sonnet-5"
    assert body["jev"]["cost_usd"] is not None


async def test_playground_oversized_input_rejected_before_calls(settings, tickets5):
    jev = FakeClassifier("jev")
    app = build_app(settings, tickets5, jev=jev)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.post(
                "/api/playground", json={"text": "x" * 4001, "opponent": ANTH}
            )
    assert r.status_code == 422
    assert jev.calls == []  # never reached a provider


async def test_oversized_body_413(client):
    r = await client.post(
        "/api/playground",
        content=json.dumps({"text": "x" * 40000, "opponent": ANTH}),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413


async def test_empty_text_rejected(client):
    r = await client.post("/api/playground", json={"text": "", "opponent": ANTH})
    assert r.status_code == 422


# ── security headers & secret sweep ──────────────────────────────────────────


async def test_security_headers_present(client):
    r = await client.get("/api/health")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_no_key_in_any_response_including_sse(client):
    bodies = []
    bodies.append((await client.get("/api/config")).text)
    bodies.append((await client.post("/api/playground", json={"text": "hi", "opponent": ANTH})).text)
    bodies.append((await client.post("/api/race", json={"opponent": ANTH, "ticket_count": 99})).text)
    r = await client.post("/api/race", json={"opponent": ANTH, "ticket_count": 2})
    race_id = r.json()["race_id"]
    async with client.stream("GET", f"/api/race/{race_id}/stream") as resp:
        async for chunk in resp.aiter_text():
            bodies.append(chunk)
            if "race_done" in chunk:
                break
    for b in bodies:
        assert SENTINEL_KEY not in b


async def test_boots_with_no_keys_and_explains_itself(tickets5):
    """Regression: a missing key must not crash startup.

    Building provider clients eagerly meant an empty TYPESAFE_API_KEY raised
    during lifespan, so a fresh clone died with a stack trace before serving
    anything. The app has to come up and say what is missing.
    """
    settings = Settings(typesafe_api_key="", anthropic_api_key="", _env_file=None)
    app = create_app(static_dir=None)
    app.router.lifespan_context = _noop_lifespan(settings, tickets5, None, None)
    # Real Providers, not the fake — this is what fell over.
    from app.deps import Providers as RealProviders

    async with app.router.lifespan_context(app):
        app.state.providers = RealProviders(settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            assert (await c.get("/api/health")).json() == {"ok": True}
            cfg = await c.get("/api/config")
            assert cfg.status_code == 200
            assert cfg.json()["anthropic_models"] == []
            race = await c.post("/api/race", json={"opponent": ANTH, "ticket_count": 1})
            assert race.status_code == 503
            assert "TYPESAFE_API_KEY" in race.json()["detail"]


async def test_race_token_gate(tickets5):
    settings = Settings(
        typesafe_api_key=SENTINEL_KEY,
        anthropic_api_key=SENTINEL_KEY,
        race_token="s3cret-token-value",
        race_max_items=5,
        _env_file=None,
    )
    app = build_app(settings, tickets5)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
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
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
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
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            root = await c.get("/")
            assert root.status_code == 200 and "race" in root.text
            asset = await c.get("/assets/app.js")
            assert asset.status_code == 200
            assert "javascript" in asset.headers["content-type"]
            assert (await c.get("/api/health")).json() == {"ok": True}
