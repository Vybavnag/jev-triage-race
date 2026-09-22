"""Anthropic Claude adapter — one messages.parse call with a Pydantic schema.

Fairness notes (also disclosed in the UI):
- effort is set to "low" on models that support it, thinking stays ENABLED
  (disabling it on Opus 5 is a known failure mode — tool text/tag leakage).
- Server-side fallbacks are deliberately NOT enabled: a silent model swap
  would corrupt the race. A refusal (HTTP 200, stop_reason == "refusal")
  scores as an error, never as a wrong label.
"""

from __future__ import annotations

import anthropic
from pydantic import BaseModel, Field

from app.classifiers import base
from app.schemas import TEAMS, Usage, Verdict

SYSTEM_PROMPT = (
    "You triage customer support messages. Classify the message exactly per "
    "the output schema: whether it is urgent (time-sensitive), which team "
    f"should handle it ({', '.join(TEAMS)}), and the customer's frustration "
    "level from 1 (calm) to 5 (furious)."
)

# Models supporting the effort parameter (Haiku 4.5 rejects it).
_EFFORT_MODELS = {"claude-opus-5", "claude-sonnet-5"}


class TriageResult(BaseModel):
    urgent: bool = Field(description="True if the message is urgent/time-sensitive")
    team: str = Field(description=f"One of: {', '.join(TEAMS)}")
    frustration: int = Field(ge=1, le=5, description="1 calm .. 5 furious")


class AnthropicClassifier:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float = 12.0):
        self._model = model
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=1, timeout=timeout
        )

    def _request_kwargs(self, text: str) -> dict:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": 512,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": text}],
        }
        if self._model in _EFFORT_MODELS:
            kwargs["output_config"] = {"effort": "low"}
        return kwargs

    def _raw_request(self, kwargs: dict) -> dict:
        return {
            **kwargs,
            "output_format": "TriageResult(urgent: bool, team: enum, frustration: 1-5)",
            "headers": dict(base.REDACTED_AUTH_HEADERS),
        }

    async def classify(self, text: str) -> Verdict:
        kwargs = self._request_kwargs(text)
        raw_request = base.finalize_raw(self._raw_request(kwargs))
        with base.Stopwatch() as sw:
            try:
                resp = await self._client.messages.parse(
                    output_format=TriageResult, **kwargs
                )
            except anthropic.RateLimitError:
                return Verdict(error=base.RATE_LIMITED, raw_request=raw_request)
            except (anthropic.APITimeoutError, anthropic.APIConnectionError):
                return Verdict(error=base.TIMEOUT, raw_request=raw_request)
            except anthropic.APIStatusError:
                return Verdict(error=base.UPSTREAM_ERROR, raw_request=raw_request)

        usage = Usage(
            input_tokens=getattr(resp.usage, "input_tokens", None),
            output_tokens=getattr(resp.usage, "output_tokens", None),
        )
        if resp.stop_reason == "refusal":
            return Verdict(
                error=base.REFUSAL, usage=usage, latency_ms=sw.ms, raw_request=raw_request
            )
        parsed: TriageResult | None = getattr(resp, "parsed_output", None)
        if parsed is None or parsed.team not in TEAMS:
            return Verdict(
                error=base.MALFORMED, usage=usage, latency_ms=sw.ms, raw_request=raw_request
            )
        return Verdict(
            urgent_p=1.0 if parsed.urgent else 0.0,
            team=parsed.team,
            frustration_raw=float(parsed.frustration),
            frustration=parsed.frustration,
            usage=usage,
            latency_ms=sw.ms,
            raw_request=raw_request,
            raw_response=base.finalize_raw(
                {
                    "model": resp.model,
                    "stop_reason": resp.stop_reason,
                    "parsed": parsed.model_dump(),
                    "usage": usage.model_dump(),
                }
            ),
        )

    async def close(self) -> None:
        await self._client.close()
