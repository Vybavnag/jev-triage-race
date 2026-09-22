"""Pure measurement helpers: label mapping and per-side latency/cost tallies.

Nothing here judges an answer. Bundled tickets carry expected labels and the
UI shows them beside each side's answer; whether an answer is right is left to
the person reading it. What the tally compares is what can be measured: how
long each side took and what it cost.

Jev semantics (verified against docs.typesafe.ai):
- Noul answers are P(true) in [0, 1]; shown as "yes" when p >= 0.5.
- Score answers are a probability-weighted float over 0-indexed levels: a
  5-level rubric returns [0.0, 4.0]. Labels are 1..5.

The score mapping deliberately avoids round(): Python rounds half-to-even
(round(0.5) == 0 but round(1.5) == 2), which is non-monotonic at the
boundaries. floor(x + 0.5) is monotonic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.schemas import Verdict

URGENT_THRESHOLD = 0.5


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


@dataclass
class SideTally:
    """Running latency/cost tally for one side of the race. Errored calls
    are counted but kept out of the latency average."""

    attempted: int = 0
    scored: int = 0
    errors: int = 0
    error_kinds: dict[str, int] = field(default_factory=dict)
    total_latency_ms: float = 0.0
    total_cost_usd: float = 0.0
    cost_known: bool = True  # flips False if any ticket had unknowable cost

    def add(self, verdict: Verdict, cost_usd: float | None) -> None:
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

    def summary(self) -> dict:
        return {
            "attempted": self.attempted,
            "scored": self.scored,
            "errors": self.errors,
            "error_kinds": self.error_kinds,
            "avg_latency_ms": (
                round(self.total_latency_ms / self.scored, 1) if self.scored else None
            ),
            "total_cost_usd": (
                round(self.total_cost_usd, 6) if self.cost_known else None
            ),
        }


def winner(jev: SideTally, llm: SideTally) -> dict:
    """Who was faster and cheaper. A side with zero scored tickets can't win
    anything; equal values are a tie."""

    def lower(j: float | None, l: float | None) -> str:
        if j is None and l is None:
            return "none"
        if j is None:
            return "llm"
        if l is None:
            return "jev"
        if j == l:
            return "tie"
        return "jev" if j < l else "llm"

    js, ls = jev.summary(), llm.summary()
    # A side that answered nothing has no latency and, for this purpose, no
    # cost either: $0 for zero answers is not "cheaper".
    j_cost = js["total_cost_usd"] if jev.scored else None
    l_cost = ls["total_cost_usd"] if llm.scored else None
    return {
        "speed": lower(js["avg_latency_ms"], ls["avg_latency_ms"]),
        "cost": lower(j_cost, l_cost),
    }
