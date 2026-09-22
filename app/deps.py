"""Shared FastAPI dependencies: settings, visitor keys, per-run providers, guards."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from app.classifiers.anthropic_llm import AnthropicClassifier
from app.classifiers.jev import JevClassifier
from app.classifiers.openai_compat import OpenAICompatClassifier
from app.config import Settings, get_settings
from app.pricing import ANTHROPIC_PRICES, anthropic_cost, jev_cost, openai_compat_cost
from app.schemas import Opponent, OpponentKind, Usage
from app.security import TokenBucket, check_race_token, client_ip

# ── Visitor keys ─────────────────────────────────────────────────────────────

TYPESAFE_KEY_HEADER = "X-TypeSafe-Key"
ANTHROPIC_KEY_HEADER = "X-Anthropic-Key"
MIN_KEY_CHARS = 8
MAX_KEY_CHARS = 512


@dataclass(frozen=True)
class VisitorKeys:
    """The provider keys one request carried. Never logged, never stored
    beyond the run they start, never echoed."""

    typesafe: str
    anthropic: str | None


def _key_header(request: Request, name: str) -> str | None:
    raw = request.headers.get(name)
    if raw is None or not raw.strip():
        return None
    key = raw.strip()
    # Shape only; the provider decides whether it is real. ASCII is required
    # because both SDKs refuse anything else, one of them by raising in the
    # constructor. The value is deliberately not repeated in the message.
    if (
        not MIN_KEY_CHARS <= len(key) <= MAX_KEY_CHARS
        or not key.isascii()
        or any(ch.isspace() or not ch.isprintable() for ch in key)
    ):
        raise HTTPException(400, detail=f"{name} is not a valid key")
    return key


def visitor_keys(request: Request) -> VisitorKeys:
    typesafe = _key_header(request, TYPESAFE_KEY_HEADER)
    if typesafe is None:
        raise HTTPException(
            400, detail=f"TypeSafe key required: send it in the {TYPESAFE_KEY_HEADER} header"
        )
    return VisitorKeys(typesafe=typesafe, anthropic=_key_header(request, ANTHROPIC_KEY_HEADER))


# ── Providers ────────────────────────────────────────────────────────────────


class Providers:
    """Classifier instances for ONE run, built from the keys the visitor
    sent. Clients are created on first use and closed by the run that owns
    them, so per-run HTTP clients never accumulate."""

    def __init__(self, settings: Settings, keys: VisitorKeys):
        self._settings = settings
        self._keys = keys
        self._jev: JevClassifier | None = None
        self._anthropic: dict[str, AnthropicClassifier] = {}
        self._openai: OpenAICompatClassifier | None = None

    @property
    def has_anthropic(self) -> bool:
        return self._keys.anthropic is not None

    @property
    def jev(self) -> JevClassifier:
        if self._jev is None:
            self._jev = JevClassifier(
                api_key=self._keys.typesafe,
                model=self._settings.jev_model,
                timeout=self._settings.per_attempt_timeout,
            )
        return self._jev

    def anthropic(self, model_id: str) -> AnthropicClassifier:
        if model_id not in self._anthropic:
            self._anthropic[model_id] = AnthropicClassifier(
                api_key=self._keys.anthropic or "",
                model=model_id,
                timeout=self._settings.per_attempt_timeout,
            )
        return self._anthropic[model_id]

    def openai_compat(self) -> OpenAICompatClassifier:
        if self._openai is None:
            self._openai = OpenAICompatClassifier(
                base_url=self._settings.openai_compat_base_url,
                api_key=self._settings.openai_compat_api_key,
                model=self._settings.openai_compat_model,
                timeout=self._settings.per_attempt_timeout,
            )
        return self._openai

    async def close(self) -> None:
        if self._jev is not None:
            await self._jev.close()
        for c in self._anthropic.values():
            await c.close()
        if self._openai:
            await self._openai.close()


def get_config(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


def get_providers(request: Request, settings: Settings = Depends(get_config)) -> Providers:
    """One run's providers from this request's keys. The factory on app
    state is the seam tests use to swap in fakes; header validation still
    runs either way."""
    keys = visitor_keys(request)
    factory = getattr(request.app.state, "provider_factory", Providers)
    return factory(settings, keys)


def resolve_opponent(opponent: Opponent, settings: Settings, providers: Providers):
    """Validate model_id against the server-side allowlist and return
    (classifier, cost_fn, label). 422 on anything not allowlisted; 400 when
    the visitor did not send the key that opponent needs."""
    if opponent.kind == OpponentKind.anthropic:
        if not providers.has_anthropic:
            raise HTTPException(
                400,
                detail=f"Anthropic key required for this opponent: send it in the {ANTHROPIC_KEY_HEADER} header",
            )
        if opponent.model_id not in ANTHROPIC_PRICES:
            raise HTTPException(422, detail="unknown model_id")
        model = opponent.model_id
        return (
            providers.anthropic(model),
            lambda usage: anthropic_cost(model, usage),
            model,
        )
    if not settings.openai_compat_enabled:
        raise HTTPException(503, detail="openai_compat opponent not configured")
    if opponent.model_id != settings.openai_compat_model:
        raise HTTPException(422, detail="unknown model_id")
    price_in, price_out = settings.openai_compat_price_in, settings.openai_compat_price_out
    return (
        providers.openai_compat(),
        lambda usage: openai_compat_cost(usage, price_in, price_out),
        settings.openai_compat_model,
    )


def jev_cost_fn(usage: Usage) -> float | None:
    return jev_cost(usage)


# ── Rate limiting ────────────────────────────────────────────────────────────
# Public-mode guards, disabled when RACE_TOKEN is set (the token is the gate)
# or when RATE_LIMITING=false (a local clone). Visitors spend their own
# provider keys, so these bound this server's compute, not anyone's wallet.
# Sizes come from Settings via configure_limits() at startup.

race_bucket = TokenBucket(capacity=3, refill_per_sec=1 / 60)
review_bucket = TokenBucket(capacity=5, refill_per_sec=2 / 60)


class ReviewSlots:
    """Process-wide cap on reviews in flight. Non-blocking: a caller that
    finds no free slot is refused with a 503, never queued, so work cannot
    pile up behind a slow provider. This one applies in RACE_TOKEN mode too:
    a token holder still runs at most `limit` at a time."""

    def __init__(self, limit: int):
        self.limit = limit
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def available(self) -> bool:
        return self._in_flight < self.limit

    def try_acquire(self) -> bool:
        if self._in_flight >= self.limit:
            return False
        self._in_flight += 1
        return True

    def release(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)

    def reset(self) -> None:
        self._in_flight = 0


review_slots = ReviewSlots(limit=2)


def configure_limits(settings: Settings) -> None:
    """Size the guards from the environment. Called once at startup."""
    race_bucket.capacity = settings.race_burst
    race_bucket.refill_per_sec = settings.race_per_minute / 60
    review_bucket.capacity = settings.review_burst
    review_bucket.refill_per_sec = settings.review_per_minute / 60
    review_slots.limit = settings.review_in_flight


def require_token(request: Request, settings: Settings) -> None:
    """Auth gate. Runs first so an unauthenticated caller can't probe anything.
    When RACE_TOKEN is unset the app is open (local-dev default)."""
    if not settings.race_token:
        return
    auth = request.headers.get("authorization", "")
    presented = auth.removeprefix("Bearer ").strip() if auth else None
    if not check_race_token(settings.race_token, presented):
        raise HTTPException(401, detail="invalid or missing token")


def consume_rate_limit(request: Request, settings: Settings, bucket: TokenBucket) -> None:
    """Per-IP bucket, public mode only. Called last, so requests rejected for
    other reasons (400/409/422) never burn a token."""
    if settings.race_token or not settings.rate_limiting:
        return
    ip = client_ip(
        request.client and (request.client.host, request.client.port),
        request.headers.get("x-forwarded-for"),
        settings.trust_proxy,
    )
    if not bucket.allow(ip):
        raise HTTPException(429, detail="rate limited", headers={"Retry-After": "60"})
