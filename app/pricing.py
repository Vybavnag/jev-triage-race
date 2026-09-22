"""Pure cost computation: USD per million tokens tables.

Cost is a headline scoreboard number, so it must be honest:
- Unknown model or missing usage => None ("unknown" in the UI), never $0.
- OpenAI-compatible prices come from operator env (price_source="env"),
  never from the client.
"""

from __future__ import annotations

from app.schemas import Usage

# ($ per MTok input, $ per MTok output). Verified 2026-09 via the claude-api
# skill; Sonnet 5's $2/$10 intro pricing ended 2026-08-31.
ANTHROPIC_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Jev: $0.042 per MTok input, output tokens free (docs.typesafe.ai).
JEV_PRICE_IN = 0.042
JEV_PRICE_OUT = 0.0

_MTOK = 1_000_000


def _cost(usage: Usage, price_in: float, price_out: float) -> float | None:
    if usage.input_tokens is None or usage.output_tokens is None:
        return None
    return (usage.input_tokens * price_in + usage.output_tokens * price_out) / _MTOK


def jev_cost(usage: Usage) -> float | None:
    return _cost(usage, JEV_PRICE_IN, JEV_PRICE_OUT)


def anthropic_cost(model_id: str, usage: Usage) -> float | None:
    prices = ANTHROPIC_PRICES.get(model_id)
    if prices is None:
        return None
    return _cost(usage, *prices)


def openai_compat_cost(
    usage: Usage, price_in: float | None, price_out: float | None
) -> float | None:
    if price_in is None or price_out is None:
        return None
    return _cost(usage, price_in, price_out)
