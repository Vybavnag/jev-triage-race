"""OpenAI-compatible adapter (OpenAI, Ollama, vLLM, ...) via plain httpx.

Base URL, key, and model come from operator env only — never from clients.
"""

from __future__ import annotations

import json

import httpx

from app.classifiers import base
from app.schemas import TEAMS, Usage, Verdict

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

    def _body(self, text: str) -> dict:
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "response_format": {"type": "json_schema", "json_schema": JSON_SCHEMA},
        }

    async def classify(self, text: str) -> Verdict:
        body = self._body(text)
        raw_request = base.finalize_raw(
            {**body, "headers": dict(base.REDACTED_AUTH_HEADERS)}
        )
        with base.Stopwatch() as sw:
            try:
                resp = await self._client.post("/chat/completions", json=body)
            except httpx.TimeoutException:
                return Verdict(error=base.TIMEOUT, raw_request=raw_request)
            except httpx.HTTPError:
                return Verdict(error=base.UPSTREAM_ERROR, raw_request=raw_request)

        if resp.status_code == 429:
            return Verdict(error=base.RATE_LIMITED, latency_ms=sw.ms, raw_request=raw_request)
        if resp.status_code >= 400:
            return Verdict(error=base.UPSTREAM_ERROR, latency_ms=sw.ms, raw_request=raw_request)

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            urgent, team, frustration = parsed["urgent"], parsed["team"], parsed["frustration"]
            if team not in TEAMS or not isinstance(frustration, int) or not (1 <= frustration <= 5):
                raise ValueError("out of schema")
            u = data.get("usage") or {}
            usage = Usage(
                input_tokens=u.get("prompt_tokens"),
                output_tokens=u.get("completion_tokens"),
            )
        except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError):
            return Verdict(error=base.MALFORMED, latency_ms=sw.ms, raw_request=raw_request)

        return Verdict(
            urgent_p=1.0 if urgent else 0.0,
            team=team,
            frustration_raw=float(frustration),
            frustration=frustration,
            usage=usage,
            latency_ms=sw.ms,
            raw_request=raw_request,
            raw_response=base.finalize_raw(
                {"model": data.get("model"), "parsed": parsed, "usage": u}
            ),
        )

    async def close(self) -> None:
        await self._client.aclose()
