"""Shared FastAPI dependencies: settings, dataset, classifier registry, guards."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.classifiers.anthropic_llm import AnthropicClassifier
from app.classifiers.jev import JevClassifier
from app.classifiers.openai_compat import OpenAICompatClassifier
from app.config import Settings, get_settings
from app.pricing import ANTHROPIC_PRICES, anthropic_cost, jev_cost, openai_compat_cost
from app.schemas import Opponent, OpponentKind, Usage
from app.security import TokenBucket, check_race_token, client_ip


class Providers:
    """Lifespan-scoped classifier instances (HTTP clients are reusable)."""

    def __init__(self, settings: Settings):
        self._settings = settings
        # Every client is built on first use. Constructing them eagerly makes a
        # missing key crash startup with a stack trace, which is the first
        # thing someone sees after cloning; the routes return a readable 503
        # instead.
        self._jev: JevClassifier | None = None
        self._anthropic: dict[str, AnthropicClassifier] = {}
        self._openai: OpenAICompatClassifier | None = None

    @property
    def jev(self) -> JevClassifier:
        if self._jev is None:
            self._jev = JevClassifier(
                api_key=self._settings.typesafe_api_key,
                model=self._settings.jev_model,
                timeout=self._settings.per_attempt_timeout,
            )
        return self._jev

    @jev.setter
    def jev(self, classifier) -> None:
        self._jev = classifier

    def anthropic(self, model_id: str) -> AnthropicClassifier:
        if model_id not in self._anthropic:
            self._anthropic[model_id] = AnthropicClassifier(
                api_key=self._settings.anthropic_api_key,
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


def resolve_opponent(opponent: Opponent, settings: Settings, providers: Providers):
    """Validate model_id against the server-side allowlist and return
    (classifier, cost_fn, label). 422 on anything not allowlisted."""
    if opponent.kind == OpponentKind.anthropic:
        if not settings.anthropic_api_key:
            raise HTTPException(503, detail="anthropic opponent not configured")
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


# Public-mode buckets, disabled when RACE_TOKEN is set — the token is the gate.
# Sized so a person demoing locally never notices them (a burst of races, then
# one a minute) while a sustained hammering of an exposed instance still costs
# far less than the per-race ticket cap would otherwise allow.
race_bucket = TokenBucket(capacity=3, refill_per_sec=1 / 60)  # burst 3, then 1/min
playground_bucket = TokenBucket(capacity=10, refill_per_sec=10 / 60)  # burst 10, then 10/min


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
    """Per-IP bucket, public mode only — when RACE_TOKEN is set the token is
    the gate. Called last, so requests rejected for other reasons (409/422)
    never burn a token."""
    if settings.race_token:
        return
    ip = client_ip(
        request.client and (request.client.host, request.client.port),
        request.headers.get("x-forwarded-for"),
        settings.trust_proxy,
    )
    if not bucket.allow(ip):
        raise HTTPException(429, detail="rate limited", headers={"Retry-After": "60"})


def get_providers(request: Request) -> Providers:
    return request.app.state.providers


def get_config(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()
