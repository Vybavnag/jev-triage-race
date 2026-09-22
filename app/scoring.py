"""Pure scoring logic: label mapping and accuracy tallies.

Jev semantics (verified against docs.typesafe.ai):
- Noul answers are P(true) in [0, 1]; predicted urgent when p >= 0.5.
- Choice answers carry a probability per option; prediction is the argmax
  (deterministic first-listed option on an exact tie).
- Score answers are a probability-weighted float over 0-indexed levels: a
  5-level rubric returns [0.0, 4.0]. Ground truth is 1..5.

The score mapping deliberately avoids round(): Python rounds half-to-even
(round(0.5) == 0 but round(1.5) == 2), which is non-monotonic at the
boundaries. floor(x + 0.5) is monotonic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.schemas import Correct, Ticket, Verdict

URGENT_THRESHOLD = 0.5
FRUSTRATION_TOLERANCE = 1  # prediction counts as correct within +/- 1 level


def map_score_to_level(score: float | None, levels: int = 5) -> int | None:
    """Map a 0-indexed Jev score float to a 1-based level, clamped."""
    if score is None or isinstance(score, float) and math.isnan(score):
        return None
    idx = math.floor(score + 0.5)
    idx = min(levels - 1, max(0, idx))
    return idx + 1


def predict_urgent(urgent_p: float | None) -> bool | None:
    if urgent_p is None:
        return None
    return urgent_p >= URGENT_THRESHOLD


def argmax_choice(probabilities: dict[str, float], order: list[str]) -> str | None:
    """Argmax over choice probabilities; ties resolve to the first option in
    the declared order so results are deterministic."""
    if not probabilities:
        return None
    best: str | None = None
    best_p = -1.0
    for option in order:
        p = probabilities.get(option)
        if p is not None and p > best_p:
            best, best_p = option, p
    return best


def grade(ticket: Ticket, verdict: Verdict) -> Correct:
    """Grade one verdict against ground truth. A None prediction is wrong
    (False), never an exception; an errored verdict grades as all-None so it
    stays out of the accuracy denominator."""
    if verdict.error is not None:
        return Correct()
    urgent_pred = predict_urgent(verdict.urgent_p)
    return Correct(
        urgent=(urgent_pred == ticket.urgent) if urgent_pred is not None else False,
        team=(verdict.team == ticket.team) if verdict.team is not None else False,
        frustration=(
            abs(verdict.frustration - ticket.frustration) <= FRUSTRATION_TOLERANCE
            if verdict.frustration is not None
            else False
        ),
    )


@dataclass
class SideTally:
    """Running accuracy/cost tally for one side of the race."""

    attempted: int = 0
    scored: int = 0
    errors: int = 0
    error_kinds: dict[str, int] = field(default_factory=dict)
    urgent_correct: int = 0
    team_correct: int = 0
    frustration_correct: int = 0
    total_latency_ms: float = 0.0
    total_cost_usd: float = 0.0
    cost_known: bool = True  # flips False if any ticket had unknowable cost

    def add(self, correct: Correct, verdict: Verdict, cost_usd: float | None) -> None:
        self.attempted += 1
        if verdict.error is not None:
            self.errors += 1
            self.error_kinds[verdict.error] = self.error_kinds.get(verdict.error, 0) + 1
            return
        self.scored += 1
        self.total_latency_ms += verdict.latency_ms
        if cost_usd is None:
            self.cost_known = False
        else:
            self.total_cost_usd += cost_usd
        self.urgent_correct += bool(correct.urgent)
        self.team_correct += bool(correct.team)
        self.frustration_correct += bool(correct.frustration)

    def summary(self) -> dict:
        def pct(n: int) -> float | None:
            return round(n / self.scored, 4) if self.scored else None

        return {
            "attempted": self.attempted,
            "scored": self.scored,
            "errors": self.errors,
            "error_kinds": self.error_kinds,
            "accuracy": {
                "urgent": pct(self.urgent_correct),
                "team": pct(self.team_correct),
                "frustration": pct(self.frustration_correct),
            },
            "avg_latency_ms": (
                round(self.total_latency_ms / self.scored, 1) if self.scored else None
            ),
            "total_cost_usd": (
                round(self.total_cost_usd, 6) if self.cost_known else None
            ),
        }


def winner(jev: SideTally, llm: SideTally) -> dict:
    """Winner semantics per dimension. A side with zero scored tickets can't
    win anything; equal values are a tie."""

    def compare(j: float | None, l: float | None, higher_wins: bool) -> str:
        if j is None and l is None:
            return "none"
        if j is None:
            return "llm"
        if l is None:
            return "jev"
        if j == l:
            return "tie"
        return ("jev" if j > l else "llm") if higher_wins else ("jev" if j < l else "llm")

    js, ls = jev.summary(), llm.summary()

    def overall_acc(s: dict) -> float | None:
        vals = [v for v in s["accuracy"].values() if v is not None]
        return sum(vals) / len(vals) if vals else None

    return {
        "speed": compare(js["avg_latency_ms"], ls["avg_latency_ms"], higher_wins=False),
        "cost": compare(js["total_cost_usd"], ls["total_cost_usd"], higher_wins=False),
        "accuracy": compare(overall_acc(js), overall_acc(ls), higher_wins=True),
    }
