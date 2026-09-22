"""Shared fixtures. No test ever touches a real API: adapters are exercised
with injected fakes or respx-mocked transports, and integration tests wire
fake classifiers into app.state."""

from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.deps import playground_bucket, race_bucket
from app.schemas import Ticket, Usage, Verdict

SENTINEL_KEY = "sk-SENTINEL-DO-NOT-LEAK-abcdefghijklmnop"


@pytest.fixture(autouse=True)
def _reset_buckets():
    race_bucket.reset()
    playground_bucket.reset()
    yield
    race_bucket.reset()
    playground_bucket.reset()


@pytest.fixture
def tickets5() -> list[Ticket]:
    return [
        Ticket(id="a1", text="site is down, urgent!", urgent=True, team="technical", frustration=4),
        Ticket(id="a2", text="please resend invoice", urgent=False, team="billing", frustration=1),
        Ticket(id="a3", text="add a user to workspace", urgent=False, team="account", frustration=1),
        Ticket(id="a4", text="pricing for 50 seats?", urgent=False, team="sales", frustration=1),
        Ticket(id="a5", text="charged twice, fix now", urgent=True, team="billing", frustration=5),
    ]


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        typesafe_api_key=SENTINEL_KEY,
        anthropic_api_key=SENTINEL_KEY,
        race_max_items=25,
        _env_file=None,
    )


class FakeClassifier:
    """Deterministic classifier for orchestrator/integration tests."""

    def __init__(
        self,
        name: str = "fake",
        delay: float = 0.0,
        fail_on: set[str] | None = None,
        fail_kind: str = "upstream_error",
        gate: asyncio.Event | None = None,
    ):
        self.name = name
        self.delay = delay
        self.fail_on = fail_on or set()
        self.fail_kind = fail_kind
        self.gate = gate
        self.live = 0
        self.max_live = 0
        self.calls: list[str] = []
        self.cancelled = 0

    async def classify(self, text: str) -> Verdict:
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        self.calls.append(text)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            if any(marker in text for marker in self.fail_on):
                return Verdict(error=self.fail_kind)
            return Verdict(
                urgent_p=0.9 if "urgent" in text or "now" in text or "down" in text else 0.1,
                team="technical",
                frustration_raw=1.0,
                frustration=2,
                usage=Usage(input_tokens=100, output_tokens=10),
                latency_ms=1.0,
            )
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.live -= 1


def flat_cost(_usage: Usage) -> float | None:
    return 0.001
