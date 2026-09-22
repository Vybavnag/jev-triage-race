"""Jev (TypeSafe AI) adapter — one system_one call, three questions."""

from __future__ import annotations

from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    Noul,
    RetryPolicy,
    Score,
    TypeSafeAPITimeoutError,
    TypeSafeError,
    TypeSafeRateLimitError,
)

from app.classifiers import base
from app.schemas import TEAMS, FrustrationLevel, TeamOption, Usage, Verdict
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

    def _raw_request(self, text: str) -> dict:
        # Built from typed fields BEFORE auth is attached — never from a
        # captured HTTP request, so no header can leak.
        return {
            "model": self._model,
            "state": text,
            "questions": {k: q.model_dump() for k, q in QUESTIONS.items()},
            "headers": dict(base.REDACTED_AUTH_HEADERS),
        }

    async def classify(self, text: str) -> Verdict:
        raw_request = self._raw_request(text)
        with base.Stopwatch() as sw:
            try:
                resp = await self._client.system_one(text, QUESTIONS)
            except TypeSafeRateLimitError:
                return Verdict(error=base.RATE_LIMITED, raw_request=base.finalize_raw(raw_request))
            except TypeSafeAPITimeoutError:
                return Verdict(error=base.TIMEOUT, raw_request=base.finalize_raw(raw_request))
            except TypeSafeError:
                return Verdict(error=base.UPSTREAM_ERROR, raw_request=base.finalize_raw(raw_request))
        # Only the upstream-shape reads are guarded: a failure here means Jev
        # returned something unexpected. Verdict/blob construction stays
        # outside so an internal bug can never masquerade as "malformed".
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
            usage = Usage(
                input_tokens=resp.usage.input_tokens if resp.usage else None,
                output_tokens=resp.usage.output_tokens if resp.usage else None,
            )
        except (KeyError, AttributeError, TypeError):
            return Verdict(
                error=base.MALFORMED,
                latency_ms=sw.ms,
                raw_request=base.finalize_raw(raw_request),
            )

        return Verdict(
            urgent_p=urgent_p,
            team=team,
            team_confidence=team_confidence,
            team_distribution=team_distribution,
            frustration_distribution=frustration_distribution,
            frustration_raw=score,
            frustration=map_score_to_level(score),
            usage=usage,
            latency_ms=sw.ms,
            raw_request=base.finalize_raw(raw_request),
            raw_response=base.finalize_raw(resp.model_dump()),
        )

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if close:
            result = close()
            if hasattr(result, "__await__"):
                await result
