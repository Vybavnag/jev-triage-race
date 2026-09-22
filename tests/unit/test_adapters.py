import json
import math
from types import SimpleNamespace

import anthropic
import httpx
import pytest
import respx
from typesafe_sdk import (
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    ScoreAnswer,
    SystemOneResponse,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
    Usage as JevUsage,
)

from app.classifiers.anthropic_llm import AnthropicClassifier, TriageResult
from app.classifiers.jev import JevClassifier
from app.classifiers.openai_compat import OpenAICompatClassifier
from app.review import REVIEW_MAX_TOKENS
from tests.conftest import SENTINEL_KEY

CODE = "password = 'hunter2'\nresult = eval(user_input)\n"  # noqa: S307 - test data, never executed
QUESTIONS = ["Is there a hardcoded secret?", "Is there a dangerous call?", "Could this be a function?"]


def jev_exc(cls):
    # SDK exception constructors want a response; __new__ sidesteps that.
    return cls.__new__(cls)


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


def jev_review_response(ps: dict[str, float]) -> SystemOneResponse:
    return SystemOneResponse(
        model="jev-1.13.0",
        usage=JevUsage(input_tokens=900, output_tokens=0),
        answers={k: NoulAnswer(type="noul", noul=p) for k, p in ps.items()},
    )


class FakeJevClient:
    def __init__(self, result=None, exc=None):
        self.result, self.exc = result, exc
        self.last_state = None
        self.last_questions = None
        self.last_kwargs = None

    async def system_one(self, state, questions, **kwargs):
        self.last_state, self.last_questions, self.last_kwargs = state, questions, kwargs
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
    v = await make_jev(exc=jev_exc(TypeSafeRateLimitError)).classify("x")
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


class TestJevReview:
    async def test_answers_follow_question_order_even_when_response_keys_do_not(self):
        c = make_jev(jev_review_response({"q2": 0.3, "q1": 0.8}))
        v = await c.review(CODE, QUESTIONS[:2])
        assert v.error is None
        assert [(a.id, a.p, a.yes) for a in v.answers] == [("q1", 0.8, True), ("q2", 0.3, False)]
        assert v.usage.input_tokens == 900

    async def test_threshold_is_inclusive(self):
        v = await make_jev(jev_review_response({"q1": 0.5})).review(CODE, QUESTIONS[:1])
        assert v.answers[0].yes is True

    async def test_sends_one_noul_per_question_with_the_real_code(self):
        c = make_jev(jev_review_response({"q1": 0.1, "q2": 0.1, "q3": 0.1}))
        await c.review(CODE, QUESTIONS)
        assert c._client.last_state == CODE
        assert c._client.last_questions == {
            "q1": Noul(instructions=QUESTIONS[0]),
            "q2": Noul(instructions=QUESTIONS[1]),
            "q3": Noul(instructions=QUESTIONS[2]),
        }

    async def test_timeout_is_passed_only_when_given(self):
        c = make_jev(jev_review_response({"q1": 0.1}))
        await c.review(CODE, QUESTIONS[:1])
        assert "timeout" not in c._client.last_kwargs
        await c.review(CODE, QUESTIONS[:1], timeout=25.0)
        assert c._client.last_kwargs["timeout"] == 25.0

    @pytest.mark.parametrize(
        "ps",
        [
            {"q1": 0.9},  # fewer answers than questions
            {"q1": 0.9, "q2": math.nan},
            {"q1": 0.9, "q2": 1.5},
            {"q1": 0.9, "q2": -0.1},
        ],
    )
    async def test_missing_or_out_of_range_answers_are_malformed(self, ps):
        v = await make_jev(jev_review_response(ps)).review(CODE, QUESTIONS[:2])
        assert v.error == "malformed"

    async def test_raw_request_omits_the_code_and_raw_response_is_allowlisted(self):
        v = await make_jev(jev_review_response({"q1": 0.8})).review(CODE, QUESTIONS[:1])
        req = json.dumps(v.raw_request)
        assert "hunter2" not in req
        assert f"<code omitted: {len(CODE)} chars>" in req
        assert v.raw_request["headers"] == {"authorization": "<redacted>"}
        assert set(v.raw_response) <= {"model", "answers", "usage"}
        assert "hunter2" not in json.dumps(v.raw_response)

    @pytest.mark.parametrize("method", ["classify", "review"])
    @pytest.mark.parametrize(
        ("exc", "kind"),
        [
            (TypeSafeRateLimitError, "rate_limited"),
            (TypeSafeAPITimeoutError, "timeout"),
            (TypeSafeAuthenticationError, "invalid_key"),
            (TypeSafePermissionDeniedError, "invalid_key"),
            (TypeSafeError, "upstream_error"),
        ],
    )
    async def test_error_mapping_is_shared_by_both_methods(self, method, exc, kind):
        # A visitor's own key being rejected must read as exactly that, not as
        # a generic upstream failure they cannot act on.
        c = make_jev(exc=jev_exc(exc))
        v = await (c.classify("x") if method == "classify" else c.review(CODE, QUESTIONS[:1]))
        assert v.error == kind


# ── Anthropic ────────────────────────────────────────────────────────────────


class FakeAnthropicResp:
    """What messages.create returns: content blocks, not a parsed object."""

    def __init__(self, stop_reason="end_turn", parsed=None, text=None, model="claude-sonnet-5"):
        self.stop_reason = stop_reason
        self.model = model
        self.usage = SimpleNamespace(input_tokens=300, output_tokens=40)
        if text is None and parsed is not None:
            text = parsed.model_dump_json()
        self.content = [] if text is None else [SimpleNamespace(type="text", text=text)]


class FakeMessages:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc = resp, exc
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.resp(kwargs) if callable(self.resp) else self.resp


def make_anthropic(resp=None, exc=None, model="claude-sonnet-5") -> AnthropicClassifier:
    c = AnthropicClassifier(api_key=SENTINEL_KEY, model=model)
    c._fake = FakeMessages(resp, exc)
    c._client = SimpleNamespace(messages=c._fake, close=None)
    return c


def answers_from_schema(values: dict[str, bool] | None = None):
    """Build the JSON the model would return for whatever schema was sent, so
    a field-name mismatch between adapter and schema fails loudly."""

    def respond(kwargs: dict) -> FakeAnthropicResp:
        props = kwargs["output_config"]["format"]["schema"]["properties"]
        body = {name: (values or {}).get(name, i % 2 == 0) for i, name in enumerate(props)}
        return FakeAnthropicResp(text=json.dumps(body))

    return respond


def anthropic_error(status: int) -> anthropic.APIStatusError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    cls = {
        429: anthropic.RateLimitError,
        401: anthropic.AuthenticationError,
        403: anthropic.PermissionDeniedError,
    }.get(status, anthropic.APIStatusError)
    return cls("boom", response=httpx.Response(status, request=req), body=None)


def anthropic_timeout() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


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


async def test_anthropic_sends_a_json_schema_output_format():
    parsed = TriageResult(urgent=True, team="billing", frustration=5)
    c = make_anthropic(FakeAnthropicResp(parsed=parsed))
    await c.classify("x")
    fmt = c._fake.calls[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert set(fmt["schema"]["required"]) == {"urgent", "team", "frustration"}
    assert fmt["schema"]["additionalProperties"] is False
    assert fmt["schema"]["properties"]["team"]["enum"] == ["billing", "technical", "account", "sales"]
    assert c._fake.calls[0]["output_config"]["effort"] == "low"


async def test_anthropic_refusal_is_error_not_label():
    v = await make_anthropic(FakeAnthropicResp(stop_reason="refusal")).classify("x")
    assert v.error == "refusal"
    assert v.team is None
    # The reason survives so the inspector can show why there is no answer.
    assert v.raw_response is not None
    assert v.raw_response["stop_reason"] == "refusal"
    assert v.raw_response["usage"]["output_tokens"] == 40


async def test_anthropic_no_text_block_is_malformed():
    v = await make_anthropic(FakeAnthropicResp(text=None)).classify("x")
    assert v.error == "malformed"


@pytest.mark.parametrize("method", ["classify", "review"])
async def test_anthropic_truncated_json_is_malformed_not_a_crash(method):
    # messages.parse would raise here before stop_reason could be read; the
    # adapter validates the text itself so a bad body is a verdict, not a 500.
    c = make_anthropic(FakeAnthropicResp(text='{"urgent": tr'))
    v = await (c.classify("x") if method == "classify" else c.review(CODE, QUESTIONS[:1]))
    assert v.error == "malformed"


@pytest.mark.parametrize("method", ["classify", "review"])
async def test_anthropic_max_tokens_stop_is_malformed(method):
    c = make_anthropic(FakeAnthropicResp(stop_reason="max_tokens", text='{"urgent": true'))
    v = await (c.classify("x") if method == "classify" else c.review(CODE, QUESTIONS[:1]))
    assert v.error == "malformed"
    # A truncation must be diagnosable: the stop reason is the whole story.
    assert v.raw_response is not None
    assert v.raw_response["stop_reason"] == "max_tokens"
    assert "hunter2" not in json.dumps(v.raw_response)


async def test_anthropic_out_of_range_frustration_is_malformed():
    v = await make_anthropic(
        FakeAnthropicResp(text='{"urgent": true, "team": "billing", "frustration": 9}')
    ).classify("x")
    assert v.error == "malformed"


async def test_anthropic_rate_limit_mapped():
    v = await make_anthropic(exc=anthropic_error(429)).classify("x")
    assert v.error == "rate_limited"
    assert v.raw_response is None  # nothing came back, so nothing to show


async def test_anthropic_effort_only_on_supported_models():
    supported = AnthropicClassifier(api_key="k", model="claude-sonnet-5")
    haiku = AnthropicClassifier(api_key="k", model="claude-haiku-4-5")
    assert supported._request_kwargs("t").get("output_config") == {"effort": "low"}
    assert "output_config" not in haiku._request_kwargs("t")


class TestAnthropicReview:
    async def test_happy_path_returns_booleans_without_probabilities(self):
        c = make_anthropic(answers_from_schema({"q1": True, "q2": False, "q3": True}))
        v = await c.review(CODE, QUESTIONS)
        assert v.error is None
        assert [(a.id, a.p, a.yes) for a in v.answers] == [
            ("q1", None, True),
            ("q2", None, False),
            ("q3", None, True),
        ]
        assert v.usage.input_tokens == 300

    async def test_questions_go_in_the_user_turn_and_the_schema_stays_fixed(self):
        c = make_anthropic(answers_from_schema())
        await c.review(CODE, QUESTIONS)
        call = c._fake.calls[0]
        user = call["messages"][0]["content"]
        assert "<code>" in user and CODE in user
        assert f"1. {QUESTIONS[0]}" in user and f"3. {QUESTIONS[2]}" in user
        assert "data" in call["system"]
        assert call["max_tokens"] == REVIEW_MAX_TOKENS
        assert call["output_config"]["effort"] == "low"
        schema = call["output_config"]["format"]["schema"]
        assert list(schema["properties"]) == ["q1", "q2", "q3"]
        assert all("description" not in prop for prop in schema["properties"].values())
        assert schema["additionalProperties"] is False

    async def test_timeout_is_passed_only_when_given(self):
        c = make_anthropic(answers_from_schema())
        await c.review(CODE, QUESTIONS[:1])
        assert "timeout" not in c._fake.calls[0]
        await c.review(CODE, QUESTIONS[:1], timeout=25.0)
        assert c._fake.calls[1]["timeout"] == 25.0

    async def test_refusal(self):
        v = await make_anthropic(FakeAnthropicResp(stop_reason="refusal")).review(CODE, QUESTIONS)
        assert v.error == "refusal"

    @pytest.mark.parametrize(
        "text",
        [
            '{"q1": true}',  # missing q2
            '{"q1": true, "q2": false, "q3": true}',  # extra key
            '{"q1": 1, "q2": false}',  # 1 is not a bool
        ],
    )
    async def test_anything_but_exactly_n_bools_is_malformed(self, text):
        v = await make_anthropic(FakeAnthropicResp(text=text)).review(CODE, QUESTIONS[:2])
        assert v.error == "malformed"

    async def test_raw_request_omits_the_code_and_raw_response_is_allowlisted(self):
        v = await make_anthropic(answers_from_schema()).review(CODE, QUESTIONS[:1])
        req = json.dumps(v.raw_request)
        assert "hunter2" not in req
        assert f"<code omitted: {len(CODE)} chars>" in req
        assert SENTINEL_KEY not in req
        assert set(v.raw_response) <= {"model", "stop_reason", "answers", "usage"}

    @pytest.mark.parametrize("method", ["classify", "review"])
    @pytest.mark.parametrize(
        ("exc", "kind"),
        [
            (anthropic_error(429), "rate_limited"),
            (anthropic_timeout(), "timeout"),
            (anthropic_error(401), "invalid_key"),
            (anthropic_error(403), "invalid_key"),
            (anthropic_error(500), "upstream_error"),
        ],
    )
    async def test_error_mapping_is_shared_by_both_methods(self, method, exc, kind):
        c = make_anthropic(exc=exc)
        v = await (c.classify("x") if method == "classify" else c.review(CODE, QUESTIONS[:1]))
        assert v.error == kind


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
@pytest.mark.parametrize("status", [401, 403])
async def test_openai_rejected_key_is_invalid_key_and_does_not_leak(status):
    respx.post(f"{BASE}/chat/completions").respond(
        status, json={"error": f"bad key {SENTINEL_KEY}"}
    )
    v = await make_openai().classify("x")
    assert v.error == "invalid_key"
    assert SENTINEL_KEY not in json.dumps(v.model_dump(), default=str)


class TestOpenAIReview:
    @respx.mock
    async def test_happy_path_and_strict_schema(self):
        route = respx.post(f"{BASE}/chat/completions").respond(
            200,
            json=chat_response(
                json.dumps({"q1": True, "q2": False}),
                usage={"prompt_tokens": 500, "completion_tokens": 12},
            ),
        )
        v = await make_openai().review(CODE, QUESTIONS[:2])
        assert v.error is None
        assert [(a.id, a.p, a.yes) for a in v.answers] == [("q1", None, True), ("q2", None, False)]
        assert v.usage.input_tokens == 500
        sent = json.loads(route.calls[0].request.content)
        fmt = sent["response_format"]["json_schema"]
        assert fmt["strict"] is True
        assert fmt["schema"]["required"] == ["q1", "q2"]
        assert fmt["schema"]["additionalProperties"] is False
        assert all(p == {"type": "boolean"} for p in fmt["schema"]["properties"].values())
        user = sent["messages"][1]["content"]
        assert "<code>" in user and CODE in user and f"1. {QUESTIONS[0]}" in user
        assert "data" in sent["messages"][0]["content"]

    @respx.mock
    @pytest.mark.parametrize(
        "content",
        ['{"q1": true}', '{"q1": true, "q2": false, "q3": true}', '{"q1": 1, "q2": false}', "nope"],
    )
    async def test_anything_but_exactly_n_bools_is_malformed(self, content):
        respx.post(f"{BASE}/chat/completions").respond(200, json=chat_response(content))
        v = await make_openai().review(CODE, QUESTIONS[:2])
        assert v.error == "malformed"

    @respx.mock
    async def test_raw_request_omits_the_code_and_raw_response_is_allowlisted(self):
        respx.post(f"{BASE}/chat/completions").respond(
            200, json=chat_response(json.dumps({"q1": True}))
        )
        v = await make_openai().review(CODE, QUESTIONS[:1])
        req = json.dumps(v.raw_request)
        assert "hunter2" not in req
        assert f"<code omitted: {len(CODE)} chars>" in req
        assert set(v.raw_response) <= {"model", "answers", "usage"}

    @respx.mock
    async def test_timeout_is_passed_through_to_the_request(self):
        route = respx.post(f"{BASE}/chat/completions").respond(
            200, json=chat_response(json.dumps({"q1": True}))
        )
        await make_openai().review(CODE, QUESTIONS[:1], timeout=25.0)
        assert route.called

    @respx.mock
    @pytest.mark.parametrize("method", ["classify", "review"])
    @pytest.mark.parametrize(
        ("status", "side_effect", "kind"),
        [
            (429, None, "rate_limited"),
            (None, httpx.ReadTimeout("boom"), "timeout"),
            (401, None, "invalid_key"),
            (500, None, "upstream_error"),
        ],
    )
    async def test_error_mapping_is_shared_by_both_methods(self, method, status, side_effect, kind):
        route = respx.post(f"{BASE}/chat/completions")
        if side_effect is not None:
            route.side_effect = side_effect
        else:
            route.respond(status, json={"error": "x"})
        c = make_openai()
        v = await (c.classify("x") if method == "classify" else c.review(CODE, QUESTIONS[:1]))
        assert v.error == kind
