import math

import pytest

from app.schemas import Usage, Verdict
from app.scoring import SideTally, map_score_to_level, predict_urgent, winner


class TestScoreMapping:
    @pytest.mark.parametrize(
        ("score", "level"),
        [
            (0.0, 1),
            (0.4, 1),
            (0.5, 2),  # round() would give 1 here (banker's -> 0); floor(+0.5) is monotonic
            (1.4, 2),
            (1.5, 3),
            (2.5, 4),  # round(2.5) == 2 would regress to level 3
            (3.5, 5),
            (4.0, 5),
            (4.4, 5),  # clamp high
            (-0.1, 1),  # clamp low
        ],
    )
    def test_boundaries_monotonic_and_clamped(self, score, level):
        assert map_score_to_level(score) == level

    def test_monotonic_over_range(self):
        levels = [map_score_to_level(x / 100) for x in range(-20, 460)]
        assert levels == sorted(levels)

    def test_none_and_nan(self):
        assert map_score_to_level(None) is None
        assert map_score_to_level(math.nan) is None


class TestUrgent:
    @pytest.mark.parametrize(
        ("p", "expected"), [(0.4999, False), (0.5, True), (0.5001, True), (None, None)]
    )
    def test_threshold(self, p, expected):
        assert predict_urgent(p) is expected


class TestTallyAndWinner:
    """The tally measures; it never judges. Latency and cost are the only
    things it compares, because a person reads the answers themselves."""

    def test_errors_excluded_from_the_latency_denominator(self):
        t = SideTally()
        t.add(Verdict(urgent_p=1.0, team="billing", frustration=3, latency_ms=10), 0.01)
        t.add(Verdict(error="rate_limited"), None)
        s = t.summary()
        assert s["attempted"] == 2 and s["scored"] == 1 and s["errors"] == 1
        assert s["avg_latency_ms"] == 10.0
        assert s["error_kinds"] == {"rate_limited": 1}
        assert "accuracy" not in s

    def test_unknown_cost_poisons_total(self):
        t = SideTally()
        t.add(Verdict(urgent_p=1.0, team="billing", frustration=3), None)
        assert t.summary()["total_cost_usd"] is None

    def test_winner_is_speed_and_cost_only(self):
        fast, slow = SideTally(), SideTally()
        fast.add(Verdict(urgent_p=1.0, team="billing", frustration=3, latency_ms=10), 0.001)
        slow.add(Verdict(urgent_p=0.0, team="sales", frustration=1, latency_ms=100), 0.01)
        assert winner(fast, slow) == {"speed": "jev", "cost": "jev"}

    def test_winner_one_sided_errors(self):
        ok, broken = SideTally(), SideTally()
        ok.add(Verdict(urgent_p=1.0, team="billing", frustration=3, latency_ms=10), 0.001)
        broken.add(Verdict(error="timeout"), None)
        assert winner(ok, broken) == {"speed": "jev", "cost": "jev"}

    def test_winner_tie_and_none(self):
        a, b = SideTally(), SideTally()
        v = Verdict(urgent_p=1.0, team="billing", frustration=3, latency_ms=10)
        for t_ in (a, b):
            t_.add(v, 0.001)
        assert winner(a, b) == {"speed": "tie", "cost": "tie"}
        assert winner(SideTally(), SideTally()) == {"speed": "none", "cost": "none"}

    def test_usage_is_not_needed_for_latency(self):
        t = SideTally()
        t.add(Verdict(urgent_p=1.0, team="billing", frustration=3, latency_ms=7, usage=Usage()), None)
        assert t.summary()["avg_latency_ms"] == 7.0
