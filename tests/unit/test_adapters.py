import json

import anthropic
import httpx
import pytest
import respx
from typesafe_sdk import (
    ChoiceAnswer,
    NoulAnswer,
    ScoreAnswer,
    SystemOneResponse,
    TypeSafeRateLimitError,
    Usage as JevUsage,
)

from app.classifiers.anthropic_llm import AnthropicClassifier, TriageResult
from app.classifiers.jev import JevClassifier
from app.classifiers.openai_compat import OpenAICompatClassifier
from tests.conftest import SENTINEL_KEY

# ── Jev ──────────────────────────────────────────────────────────────────────


FRUSTRATION_LEGEND = {
    0: "calm and neutral",
    1: "mildly annoyed",
    2: "clearly frustrated",
    3: "angry",
    4: "furious or threatening to leave",
}


def jev_response(
    noul=0.97,
    choice="technical",
    score=2.4,
    team_probabilities=None,
    frustration_probabilities=None,
    legend=None,
):
    # The SDK's answer models are frozen, so every field a test wants to vary
    # has to be passed in at construction rather than assigned afterwards.
    return SystemOneResponse(
        model="jev-1.13.0",
        usage=JevUsage(input_tokens=180, output_tokens=0),
        answers={
            "is_urgent": NoulAnswer(type="noul", noul=noul),
            "team": ChoiceAnswer(
                type="choice",
                choice=choice,
                confidence=0.8,
                probabilities=(
                    {"technical": 0.8, "billing": 0.2}
                    if team_probabilities is None
                    else team_probabilities
                ),
            ),
            "frustration": ScoreAnswer(
                type="score",
                score=score,
                confidence=0.5,
                legend=FRUSTRATION_LEGEND if legend is None else legend,
                probabilities=(
                    {i: 0.2 for i in range(5)}
                    if frustration_probabilities is None
                    else frustration_probabilities
                ),
            ),
        },
    )


class FakeJevClient:
    def __init__(self, result=None, exc=None):
        self.result, self.exc = result, exc

    async def system_one(self, state, questions):
        if self.exc:
            raise self.exc
        return self.result


def make_jev(result=None, exc=None) -> JevClassifier:
    c = JevClassifier(api_key=SENTINEL_KEY)
    c._client = FakeJevClient(result, exc)
    return c


async def test_jev_happy_path():
    v = await make_jev(jev_response()).classify("site down")
    assert v.error is None
    assert v.urgent_p == 0.97
    assert v.team == "technical"
    assert v.frustration == 3  # floor(2.4 + 0.5) = 2 -> level 3
    assert v.usage.input_tokens == 180
    assert v.latency_ms >= 0


async def test_jev_captures_team_distribution_ranked():
    """The distribution is the thing an LLM cannot return, so it must survive
    the adapter. Nominal options are ranked most-likely-first."""
    resp = jev_response(
        choice="technical",
        team_probabilities={
            "billing": 0.19,
            "technical": 0.76,
            "account": 0.04,
            "sales": 0.01,
        },
    )
    v = await make_jev(resp).classify("x")
    dist = v.team_distribution
    assert dist is not None
    assert [o.option for o in dist] == ["technical", "billing", "account", "sales"]
    assert dist[0].p == pytest.approx(0.76)
    assert sum(o.p for o in dist) == pytest.approx(1.0)


async def test_jev_frustration_distribution_keeps_scale_order():
    """Frustration is an ordinal scale: sorting it by probability would
    misrepresent it. Levels surface 1-based with the API's own legend text."""
    resp = jev_response(
        frustration_probabilities={0: 0.05, 1: 0.17, 2: 0.42, 3: 0.28, 4: 0.08}
    )
    v = await make_jev(resp).classify("x")
    dist = v.frustration_distribution
    assert dist is not None
    assert [l.level for l in dist] == [1, 2, 3, 4, 5]  # scale order, not ranked
    assert dist[0].label == "calm and neutral"
    assert dist[2].p == pytest.approx(0.42)


async def test_jev_missing_distributions_are_none_not_empty():
    resp = jev_response(team_probabilities={}, frustration_probabilities={})
    v = await make_jev(resp).classify("x")
    assert v.error is None
    assert v.team_distribution is None
    assert v.frustration_distribution is None


async def test_jev_rate_limited():
    exc = TypeSafeRateLimitError.__new__(TypeSafeRateLimitError)
    v = await make_jev(exc=exc).classify("x")
    assert v.error == "rate_limited"


async def test_jev_malformed_answers():
    resp = jev_response()
    resp.answers.pop("team")
    v = await make_jev(resp).classify("x")
    assert v.error == "malformed"


async def test_jev_raw_request_has_no_auth():
    v = await make_jev(jev_response()).classify("hello")
    dumped = json.dumps(v.raw_request)
    assert SENTINEL_KEY not in dumped
    assert v.raw_request["headers"] == {"authorization": "<redacted>"}


# ── Anthropic ────────────────────────────────────────────────────────────────


class FakeAnthropicResp:
    def __init__(self, stop_reason="end_turn", parsed=None, model="claude-sonnet-5"):
        self.stop_reason = stop_reason
        self.parsed_output = parsed
        self.model = model
        self.usage = type("U", (), {"input_tokens": 300, "output_tokens": 40})()


def make_anthropic(resp=None, exc=None, model="claude-sonnet-5") -> AnthropicClassifier:
    c = AnthropicClassifier(api_key=SENTINEL_KEY, model=model)

    class FakeMessages:
        async def parse(self, **kwargs):
            if exc:
                raise exc
            return resp

    c._client = type("FC", (), {"messages": FakeMessages(), "close": None})()
    return c


async def test_anthropic_happy_path():
    parsed = TriageResult(urgent=True, team="billing", frustration=5)
    v = await make_anthropic(FakeAnthropicResp(parsed=parsed)).classify("x")
    assert v.error is None
    assert v.urgent_p == 1.0 and v.team == "billing" and v.frustration == 5
    assert v.usage.output_tokens == 40
    # The LLM asserts one value per field; there is no distribution to report.
    assert v.team_distribution is None
    assert v.frustration_distribution is None
    assert v.team_confidence is None


async def test_anthropic_refusal_is_error_not_label():
    v = await make_anthropic(FakeAnthropicResp(stop_reason="refusal")).classify("x")
    assert v.error == "refusal"
    assert v.team is None


async def test_anthropic_parsed_none_malformed():
    v = await make_anthropic(FakeAnthropicResp(parsed=None)).classify("x")
    assert v.error == "malformed"


async def test_anthropic_rate_limit_mapped():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.RateLimitError(
        "rate limited", response=httpx.Response(429, request=req), body=None
    )
    v = await make_anthropic(exc=exc).classify("x")
    assert v.error == "rate_limited"


async def test_anthropic_effort_only_on_supported_models():
    supported = AnthropicClassifier(api_key="k", model="claude-sonnet-5")
    haiku = AnthropicClassifier(api_key="k", model="claude-haiku-4-5")
    assert supported._request_kwargs("t").get("output_config") == {"effort": "low"}
    assert "output_config" not in haiku._request_kwargs("t")


# ── OpenAI-compatible ────────────────────────────────────────────────────────

BASE = "https://llm.example.com/v1"


def make_openai() -> OpenAICompatClassifier:
    return OpenAICompatClassifier(base_url=BASE, api_key=SENTINEL_KEY, model="gpt-x")


def chat_response(content: str, usage: dict | None = None) -> dict:
    body = {"model": "gpt-x", "choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return body


@respx.mock
async def test_openai_happy_path():
    respx.post(f"{BASE}/chat/completions").respond(
        200,
        json=chat_response(
            json.dumps({"urgent": True, "team": "sales", "frustration": 2}),
            usage={"prompt_tokens": 120, "completion_tokens": 15},
        ),
    )
    v = await make_openai().classify("x")
    assert v.error is None
    assert v.team == "sales" and v.urgent_p == 1.0
    assert v.usage.input_tokens == 120


@respx.mock
async def test_openai_usage_absent_is_none():
    respx.post(f"{BASE}/chat/completions").respond(
        200, json=chat_response(json.dumps({"urgent": False, "team": "billing", "frustration": 1}))
    )
    v = await make_openai().classify("x")
    assert v.error is None
    assert v.usage.input_tokens is None and v.usage.output_tokens is None


@respx.mock
async def test_openai_non_json_content_malformed():
    respx.post(f"{BASE}/chat/completions").respond(200, json=chat_response("sure, here you go!"))
    v = await make_openai().classify("x")
    assert v.error == "malformed"


@respx.mock
async def test_openai_out_of_schema_malformed():
    respx.post(f"{BASE}/chat/completions").respond(
        200, json=chat_response(json.dumps({"urgent": True, "team": "hr", "frustration": 9}))
    )
    v = await make_openai().classify("x")
    assert v.error == "malformed"


@respx.mock
async def test_openai_429_and_timeout():
    route = respx.post(f"{BASE}/chat/completions")
    route.respond(429, json={"error": "slow down"})
    assert (await make_openai().classify("x")).error == "rate_limited"
    route.side_effect = httpx.ReadTimeout("boom")
    assert (await make_openai().classify("x")).error == "timeout"


@respx.mock
async def test_openai_401_body_does_not_leak_key():
    respx.post(f"{BASE}/chat/completions").respond(
        401, json={"error": f"bad key {SENTINEL_KEY}"}
    )
    v = await make_openai().classify("x")
    assert v.error == "upstream_error"
    assert SENTINEL_KEY not in json.dumps(v.model_dump(), default=str)
