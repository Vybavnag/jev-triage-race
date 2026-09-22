"""Shared fixtures. No test ever touches a real API: adapters are exercised
with injected fakes or respx-mocked transports, and integration tests wire
fake classifiers into app.state."""

from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.deps import configure_limits, race_bucket, review_bucket, review_slots
from app.schemas import ReviewAnswer, ReviewVerdict, Ticket, Usage, Verdict

# The keys a test visitor sends. Two distinct sentinels so a sweep can prove
# neither the TypeSafe nor the Anthropic key ever appears in a response.
SENTINEL_KEY = "sk-SENTINEL-DO-NOT-LEAK-abcdefghijklmnop"
SENTINEL_ANTHROPIC = "sk-ant-SENTINEL-DO-NOT-LEAK-qrstuvwxyz0123"
KEYS = {"X-TypeSafe-Key": SENTINEL_KEY, "X-Anthropic-Key": SENTINEL_ANTHROPIC}


@pytest.fixture(autouse=True)
def _reset_buckets():
    configure_limits(Settings(_env_file=None))
    race_bucket.reset()
    review_bucket.reset()
    review_slots.reset()
    yield
    configure_limits(Settings(_env_file=None))
    race_bucket.reset()
    review_bucket.reset()
    review_slots.reset()


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
    return Settings(race_max_items=25, _env_file=None)


class FakeProviders:
    """One run's classifiers, swapped for fakes. Records the keys it was
    built with and whether the run closed it, which the real one must do so
    per-run HTTP clients never accumulate."""

    def __init__(self, settings, keys, jev=None, llm=None):
        self.settings = settings
        self.keys = keys
        self.jev = jev or FakeClassifier("jev")
        self._llm = llm or FakeClassifier("llm")
        self.closed = False

    @property
    def has_anthropic(self) -> bool:
        return self.keys.anthropic is not None

    def anthropic(self, model_id: str):
        return self._llm

    def openai_compat(self):
        return self._llm

    async def close(self) -> None:
        self.closed = True


class FakeClassifier:
    """Deterministic classifier + reviewer for orchestrator/integration tests.

    Review answers: "yes" to every question when the code contains "TODO".
    A probabilistic fake (Jev-like) reports p=0.8/0.2; otherwise (LLM-like)
    p is None and only the boolean is set.
    """

    def __init__(
        self,
        name: str = "fake",
        delay: float = 0.0,
        fail_on: set[str] | None = None,
        fail_kind: str = "upstream_error",
        gate: asyncio.Event | None = None,
        probabilistic: bool = True,
        review_raises: bool = False,
    ):
        self.name = name
        self.delay = delay
        self.fail_on = fail_on or set()
        self.fail_kind = fail_kind
        self.gate = gate
        self.probabilistic = probabilistic
        self.review_raises = review_raises
        self.live = 0
        self.max_live = 0
        self.calls: list[str] = []
        self.review_calls: list[tuple[str, list[str], float | None]] = []
        self.cancelled = 0

    async def _wait(self) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)

    async def classify(self, text: str) -> Verdict:
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        self.calls.append(text)
        try:
            await self._wait()
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

    async def review(
        self, code: str, questions, *, timeout: float | None = None
    ) -> ReviewVerdict:
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        self.review_calls.append((code, list(questions), timeout))
        try:
            await self._wait()
            if self.review_raises:
                raise RuntimeError("provider blew up")
            if any(marker in code for marker in self.fail_on):
                return ReviewVerdict(error=self.fail_kind)
            hit = "TODO" in code
            answers = []
            for i, _ in enumerate(questions, start=1):
                if self.probabilistic:
                    p = 0.8 if hit else 0.2
                    answers.append(ReviewAnswer(id=f"q{i}", p=p, yes=p >= 0.5))
                else:
                    answers.append(ReviewAnswer(id=f"q{i}", p=None, yes=hit))
            return ReviewVerdict(
                answers=answers, usage=Usage(input_tokens=100, output_tokens=10), latency_ms=1.0
            )
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.live -= 1


def flat_cost(_usage: Usage) -> float | None:
    return 0.001
