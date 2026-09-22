"""Jev (TypeSafe AI) adapter — one system_one call answers every question.

Triage asks three fixed questions (a Noul, a Choice, a Score). Review asks
one Noul per user question, so what comes back is a probability per
question rather than a bare yes/no.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    Noul,
    Question,
    RetryPolicy,
    Score,
    SystemOneResponse,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
)

from app.classifiers import base
from app.review import code_placeholder, question_ids, yes_from_p
from app.schemas import (
    FrustrationLevel,
    ReviewAnswer,
    ReviewVerdict,
    TeamOption,
    Usage,
    Verdict,
)
from app.scoring import map_score_to_level

# Score criteria are an ordered list of level descriptions; the answer is a
# probability-weighted float over 0-indexed levels (5 levels -> 0.0..4.0).
FRUSTRATION_CRITERIA = [
    "calm and neutral",
    "mildly annoyed",
    "clearly frustrated",
    "angry",
    "furious or threatening to leave",
]

# Choice criteria are a mapping of option -> what that option means.
TEAM_CRITERIA = {
    "billing": "invoices, charges, refunds, payment methods, pricing on an existing account",
    "technical": "bugs, outages, API and integration problems, performance, data issues",
    "account": "access, logins, users and permissions, workspace settings, data requests",
    "sales": "pre-purchase questions, plans and quotes, demos, contracts and renewals",
}

QUESTIONS = {
    "is_urgent": Noul(
        instructions="The message conveys urgency or time-sensitivity."
    ),
    "team": Choice(
        instructions="Which team should handle this support message?",
        criteria=TEAM_CRITERIA,
    ),
    "frustration": Score(
        instructions="How frustrated does the customer appear?",
        criteria=FRUSTRATION_CRITERIA,
    ),
}


def _team_distribution(probabilities: dict | None) -> list[TeamOption] | None:
    """Nominal categories, so rank them: most likely option first."""
    if not probabilities:
        return None
    ranked = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)
    return [TeamOption(option=str(name), p=float(p)) for name, p in ranked]


def _frustration_distribution(
    probabilities: dict | None, legend: dict | None
) -> list[FrustrationLevel] | None:
    """An ordered scale, so keep scale order — sorting by probability would
    misrepresent it. Keys arrive 0-indexed; surface them 1..5 to match the
    labels the rest of the app grades against, and take the wording from the
    API's own legend rather than restating it here."""
    if not probabilities:
        return None
    legend = legend or {}
    return [
        FrustrationLevel(
            level=int(idx) + 1,
            label=str(legend.get(idx) or legend.get(str(idx)) or f"level {int(idx) + 1}"),
            p=float(p),
        )
        for idx, p in sorted(probabilities.items(), key=lambda kv: int(kv[0]))
    ]


def _probability(value: object) -> float:
    """A Noul answer must be a real number in [0, 1]; anything else is a
    malformed response, not a probability to act on."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("not a number")
    p = float(value)
    if math.isnan(p) or not 0.0 <= p <= 1.0:
        raise ValueError("out of range")
    return p


class JevClassifier:
    name = "jev"

    def __init__(self, api_key: str, model: str = "jev-latest", timeout: float = 12.0):
        self._model = model
        self._client = AsyncTypeSafeClient(
            api_key=api_key,
            model=model,
            retry=RetryPolicy(max_retries=1),
            timeout=timeout,
        )

    def _raw_request(self, state: str, questions: Mapping[str, Question]) -> dict:
        # Built from typed fields BEFORE auth is attached — never from a
        # captured HTTP request, so no header can leak.
        return {
            "model": self._model,
            "state": state,
            "questions": {k: q.model_dump() for k, q in questions.items()},
            "headers": dict(base.REDACTED_AUTH_HEADERS),
        }

    async def _call(
        self,
        state: str,
        questions: Mapping[str, Question],
        *,
        timeout: float | None = None,
    ) -> base.CallResult[SystemOneResponse]:
        options = {"timeout": timeout} if timeout is not None else {}
        with base.Stopwatch() as sw:
            try:
                resp = await self._client.system_one(state, questions, **options)
            except TypeSafeRateLimitError:
                return base.CallResult(None, base.RATE_LIMITED)
            except TypeSafeAPITimeoutError:
                return base.CallResult(None, base.TIMEOUT)
            except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError):
                return base.CallResult(None, base.INVALID_KEY)
            except TypeSafeError:
                return base.CallResult(None, base.UPSTREAM_ERROR)
        reported = getattr(resp, "usage", None)
        usage = Usage(
            input_tokens=getattr(reported, "input_tokens", None),
            output_tokens=getattr(reported, "output_tokens", None),
        )
        return base.CallResult(resp, None, usage, sw.ms, {"model": getattr(resp, "model", None)})

    async def classify(self, text: str) -> Verdict:
        raw_request = base.finalize_raw(self._raw_request(text, QUESTIONS))
        call = await self._call(text, QUESTIONS)
        if call.error is not None:
            return base.failed(Verdict, call, raw_request)
        resp = call.payload
        # Only the upstream-shape reads are guarded: a failure here means Jev
        # returned something unexpected. Verdict construction stays outside so
        # an internal bug can never masquerade as "malformed".
        try:
            answers = resp.answers
            urgent_p = answers["is_urgent"].noul
            team_ans = answers["team"]
            frustration_ans = answers["frustration"]
            score = frustration_ans.score
            team, team_confidence = team_ans.choice, team_ans.confidence
            team_distribution = _team_distribution(team_ans.probabilities)
            frustration_distribution = _frustration_distribution(
                frustration_ans.probabilities, frustration_ans.legend
            )
        except (KeyError, AttributeError, TypeError):
            return base.failed(Verdict, call, raw_request, base.MALFORMED)

        return Verdict(
            urgent_p=urgent_p,
            team=team,
            team_confidence=team_confidence,
            team_distribution=team_distribution,
            frustration_distribution=frustration_distribution,
            frustration_raw=score,
            frustration=map_score_to_level(score),
            usage=call.usage,
            latency_ms=call.latency_ms,
            raw_request=raw_request,
            raw_response=base.finalize_raw(resp.model_dump()),
        )

    async def review(
        self, code: str, questions: Sequence[str], *, timeout: float | None = None
    ) -> ReviewVerdict:
        ids = question_ids(len(questions))
        asked = {qid: Noul(instructions=q) for qid, q in zip(ids, questions, strict=True)}
        raw_request = base.finalize_raw(self._raw_request(code_placeholder(code), asked))
        call = await self._call(code, asked, timeout=timeout)
        if call.error is not None:
            return base.failed(ReviewVerdict, call, raw_request)
        try:
            answers = [
                ReviewAnswer(id=qid, p=p, yes=yes_from_p(p))
                for qid in ids
                for p in (_probability(call.payload.answers[qid].noul),)
            ]
        except (KeyError, AttributeError, TypeError, ValueError):
            return base.failed(ReviewVerdict, call, raw_request, base.MALFORMED)
        return ReviewVerdict(
            answers=answers,
            usage=call.usage,
            latency_ms=call.latency_ms,
            raw_request=raw_request,
            # Allowlisted: the answers, never the echoed state.
            raw_response=base.finalize_raw(
                {
                    **call.meta,
                    "answers": {a.id: {"noul": a.p} for a in answers},
                    "usage": call.usage.model_dump(),
                }
            ),
        )

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if close:
            result = close()
            if hasattr(result, "__await__"):
                await result
