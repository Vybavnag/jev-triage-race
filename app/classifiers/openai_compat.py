"""OpenAI-compatible adapter (OpenAI, Ollama, vLLM, ...) via plain httpx.

Base URL, key, and model come from operator env only — never from clients.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import httpx

from app.classifiers import base
from app.review import (
    SYSTEM_PROMPT as REVIEW_SYSTEM_PROMPT,
    build_prompt,
    question_ids,
    redacted_prompt,
    review_json_schema,
    review_model,
)
from app.schemas import TEAMS, ReviewAnswer, ReviewVerdict, Usage, Verdict

JSON_SCHEMA = {
    "name": "triage_result",
    "schema": {
        "type": "object",
        "properties": {
            "urgent": {"type": "boolean"},
            "team": {"type": "string", "enum": list(TEAMS)},
            "frustration": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["urgent", "team", "frustration"],
        "additionalProperties": False,
    },
    "strict": True,
}

SYSTEM_PROMPT = (
    "You triage customer support messages. Respond ONLY with JSON per the "
    "schema: urgent (bool), team (one of "
    f"{', '.join(TEAMS)}), frustration (1 calm .. 5 furious)."
)


class OpenAICompatClassifier:
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 12.0):
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=timeout,
            transport=httpx.AsyncHTTPTransport(retries=1),
        )

    def _body(self, system: str, user: str, json_schema: dict) -> dict:
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_schema", "json_schema": json_schema},
        }

    async def _call(self, body: dict, *, timeout: float | None = None) -> base.CallResult[str]:
        """One chat completion. Returns the assistant content for the caller
        to validate, or an error kind."""
        options = {"timeout": timeout} if timeout is not None else {}
        with base.Stopwatch() as sw:
            try:
                resp = await self._client.post("/chat/completions", json=body, **options)
            except httpx.TimeoutException:
                return base.CallResult(None, base.TIMEOUT)
            except httpx.HTTPError:
                return base.CallResult(None, base.UPSTREAM_ERROR)
        if resp.status_code == 429:
            return base.CallResult(None, base.RATE_LIMITED, latency_ms=sw.ms)
        if resp.status_code in (401, 403):
            return base.CallResult(None, base.INVALID_KEY, latency_ms=sw.ms)
        if resp.status_code >= 400:
            return base.CallResult(None, base.UPSTREAM_ERROR, latency_ms=sw.ms)
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("content is not text")
        except (ValueError, KeyError, IndexError, TypeError):
            return base.CallResult(None, base.MALFORMED, latency_ms=sw.ms)
        reported = data.get("usage") or {}
        usage = Usage(
            input_tokens=reported.get("prompt_tokens"),
            output_tokens=reported.get("completion_tokens"),
        )
        return base.CallResult(content, None, usage, sw.ms, {"model": data.get("model")})

    async def classify(self, text: str) -> Verdict:
        body = self._body(SYSTEM_PROMPT, text, JSON_SCHEMA)
        raw_request = base.finalize_raw({**body, "headers": dict(base.REDACTED_AUTH_HEADERS)})
        call = await self._call(body)
        if call.error is not None:
            return base.failed(Verdict, call, raw_request)
        try:
            parsed = json.loads(call.payload)
            urgent, team, frustration = parsed["urgent"], parsed["team"], parsed["frustration"]
            if (
                not isinstance(urgent, bool)
                or team not in TEAMS
                or isinstance(frustration, bool)
                or not isinstance(frustration, int)
                or not 1 <= frustration <= 5
            ):
                raise ValueError("out of schema")
        except (KeyError, ValueError, TypeError):
            return base.failed(Verdict, call, raw_request, base.MALFORMED)
        return Verdict(
            urgent_p=1.0 if urgent else 0.0,
            team=team,
            frustration_raw=float(frustration),
            frustration=frustration,
            usage=call.usage,
            latency_ms=call.latency_ms,
            raw_request=raw_request,
            raw_response=base.finalize_raw(
                {**call.meta, "parsed": parsed, "usage": call.usage.model_dump()}
            ),
        )

    async def review(
        self, code: str, questions: Sequence[str], *, timeout: float | None = None
    ) -> ReviewVerdict:
        n = len(questions)
        json_schema = {"name": "review_result", "schema": review_json_schema(n), "strict": True}
        body = self._body(REVIEW_SYSTEM_PROMPT, build_prompt(code, questions), json_schema)
        shown = self._body(REVIEW_SYSTEM_PROMPT, redacted_prompt(code, questions), json_schema)
        raw_request = base.finalize_raw({**shown, "headers": dict(base.REDACTED_AUTH_HEADERS)})
        call = await self._call(body, timeout=timeout)
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
        await self._client.aclose()
