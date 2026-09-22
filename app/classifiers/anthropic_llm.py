"""Anthropic Claude adapter — one messages.create with a JSON-schema output.

Why not messages.parse: it validates every text block before the caller can
read stop_reason, so a refusal with partial text or a max_tokens truncation
raises instead of reporting. This adapter reads stop_reason first and then
validates the JSON itself, so both cases are verdicts (`refusal`,
`malformed`) rather than exceptions.

Fairness notes (also disclosed in the UI):
- effort is set to "low" on models that support it, thinking stays ENABLED
  (disabling it on Opus 5 is a known failure mode — tool text/tag leakage).
- Server-side fallbacks are deliberately NOT enabled: a silent model swap
  would corrupt the race. A refusal (HTTP 200, stop_reason == "refusal")
  scores as an error, never as a wrong label.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from app.classifiers import base
from app.review import (
    REVIEW_MAX_TOKENS,
    SYSTEM_PROMPT as REVIEW_SYSTEM_PROMPT,
    build_prompt,
    question_ids,
    redacted_prompt,
    review_json_schema,
    review_model,
)
from app.schemas import TEAMS, ReviewAnswer, ReviewVerdict, Usage, Verdict

SYSTEM_PROMPT = (
    "You triage customer support messages. Classify the message exactly per "
    "the output schema: whether it is urgent (time-sensitive), which team "
    f"should handle it ({', '.join(TEAMS)}), and the customer's frustration "
    "level from 1 (calm) to 5 (furious)."
)

# Models supporting the effort parameter (Haiku 4.5 rejects it).
_EFFORT_MODELS = {"claude-opus-5", "claude-sonnet-5"}

TRIAGE_MAX_TOKENS = 512


class TriageResult(BaseModel):
    urgent: bool = Field(description="True if the message is urgent/time-sensitive")
    team: Literal["billing", "technical", "account", "sales"] = Field(
        description="The team that should handle the message"
    )
    frustration: int = Field(ge=1, le=5, description="1 calm .. 5 furious")


# The SDK's own transform: additionalProperties false, numeric bounds folded
# into the description. Pydantic still enforces the bounds on validation.
TRIAGE_SCHEMA = anthropic.transform_schema(TriageResult)


class AnthropicClassifier:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float = 12.0):
        self._model = model
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=1, timeout=timeout
        )

    def _request_kwargs(
        self, user: str, *, system: str = SYSTEM_PROMPT, max_tokens: int = TRIAGE_MAX_TOKENS
    ) -> dict:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self._model in _EFFORT_MODELS:
            kwargs["output_config"] = {"effort": "low"}
        return kwargs

    def _raw_request(self, kwargs: dict, output_format: str) -> dict:
        return {
            **kwargs,
            "output_format": output_format,
            "headers": dict(base.REDACTED_AUTH_HEADERS),
        }

    async def _call(
        self, kwargs: dict, schema: dict, *, timeout: float | None = None
    ) -> base.CallResult[str]:
        """One messages.create. Returns the JSON text for the caller to
        validate, or an error kind: SDK failures, a refusal, a truncated
        body, or a response with no text block."""
        output_config = {
            **kwargs.get("output_config", {}),
            "format": {"type": "json_schema", "schema": schema},
        }
        request = {**kwargs, "output_config": output_config}
        if timeout is not None:
            request["timeout"] = timeout
        with base.Stopwatch() as sw:
            try:
                resp = await self._client.messages.create(**request)
            except anthropic.RateLimitError:
                return base.CallResult(None, base.RATE_LIMITED)
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
                return base.CallResult(None, base.INVALID_KEY)
            except (anthropic.APITimeoutError, anthropic.APIConnectionError):
                return base.CallResult(None, base.TIMEOUT)
            except anthropic.APIStatusError:
                return base.CallResult(None, base.UPSTREAM_ERROR)
        usage = Usage(
            input_tokens=getattr(resp.usage, "input_tokens", None),
            output_tokens=getattr(resp.usage, "output_tokens", None),
        )
        meta = {"model": getattr(resp, "model", None), "stop_reason": resp.stop_reason}
        if resp.stop_reason == "refusal":
            return base.CallResult(None, base.REFUSAL, usage, sw.ms, meta)
        text = next(
            (block.text for block in resp.content if getattr(block, "type", None) == "text"),
            None,
        )
        if resp.stop_reason == "max_tokens" or text is None:
            return base.CallResult(None, base.MALFORMED, usage, sw.ms, meta)
        return base.CallResult(text, None, usage, sw.ms, meta)

    async def classify(self, text: str) -> Verdict:
        kwargs = self._request_kwargs(text)
        raw_request = base.finalize_raw(
            self._raw_request(kwargs, "TriageResult(urgent: bool, team: enum, frustration: 1-5)")
        )
        call = await self._call(kwargs, TRIAGE_SCHEMA)
        if call.error is not None:
            return base.failed(Verdict, call, raw_request)
        parsed = base.validate_json(TriageResult, call.payload)
        if parsed is None:
            return base.failed(Verdict, call, raw_request, base.MALFORMED)
        return Verdict(
            urgent_p=1.0 if parsed.urgent else 0.0,
            team=parsed.team,
            frustration_raw=float(parsed.frustration),
            frustration=parsed.frustration,
            usage=call.usage,
            latency_ms=call.latency_ms,
            raw_request=raw_request,
            raw_response=base.finalize_raw(
                {**call.meta, "parsed": parsed.model_dump(), "usage": call.usage.model_dump()}
            ),
        )

    async def review(
        self, code: str, questions: Sequence[str], *, timeout: float | None = None
    ) -> ReviewVerdict:
        n = len(questions)
        kwargs = self._request_kwargs(
            build_prompt(code, questions),
            system=REVIEW_SYSTEM_PROMPT,
            max_tokens=REVIEW_MAX_TOKENS,
        )
        shown = {**kwargs, "messages": [{"role": "user", "content": redacted_prompt(code, questions)}]}
        raw_request = base.finalize_raw(self._raw_request(shown, f"ReviewResult(q1..q{n}: bool)"))
        call = await self._call(kwargs, review_json_schema(n), timeout=timeout)
        if call.error is not None:
            return base.failed(ReviewVerdict, call, raw_request)
        parsed = base.validate_json(review_model(n), call.payload)
        if parsed is None:
            return base.failed(ReviewVerdict, call, raw_request, base.MALFORMED)
        answers = [ReviewAnswer(id=qid, p=None, yes=getattr(parsed, qid)) for qid in question_ids(n)]
        return ReviewVerdict(
            answers=answers,
            usage=call.usage,
            latency_ms=call.latency_ms,
            raw_request=raw_request,
            raw_response=base.finalize_raw(
                {**call.meta, "answers": parsed.model_dump(), "usage": call.usage.model_dump()}
            ),
        )

    async def close(self) -> None:
        await self._client.close()
