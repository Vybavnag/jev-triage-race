import math

import pytest

from app.schemas import Ticket, Usage, Verdict
from app.scoring import (
    SideTally,
    argmax_choice,
    grade,
    map_score_to_level,
    predict_urgent,
    winner,
)

TICKET = Ticket(id="t", text="x", urgent=True, team="billing", frustration=3)


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


class TestArgmax:
    def test_picks_max(self):
        assert argmax_choice({"a": 0.1, "b": 0.8, "c": 0.1}, ["a", "b", "c"]) == "b"

    def test_tie_is_first_in_declared_order(self):
        assert argmax_choice({"a": 0.5, "b": 0.5}, ["a", "b"]) == "a"
        assert argmax_choice({"a": 0.5, "b": 0.5}, ["b", "a"]) == "b"

    def test_empty(self):
        assert argmax_choice({}, ["a"]) is None


class TestGrade:
    def test_correct_all(self):
        v = Verdict(urgent_p=0.9, team="billing", frustration=3)
        c = grade(TICKET, v)
        assert (c.urgent, c.team, c.frustration) == (True, True, True)

    def test_frustration_tolerance_inclusive(self):
        assert grade(TICKET, Verdict(urgent_p=1.0, team="billing", frustration=4)).frustration is True
        assert grade(TICKET, Verdict(urgent_p=1.0, team="billing", frustration=5)).frustration is False

    def test_none_prediction_is_wrong_not_exception(self):
        c = grade(TICKET, Verdict())
        assert (c.urgent, c.team, c.frustration) == (False, False, False)

    def test_errored_verdict_grades_all_none(self):
        c = grade(TICKET, Verdict(error="timeout"))
        assert (c.urgent, c.team, c.frustration) == (None, None, None)


class TestTallyAndWinner:
    def test_errors_excluded_from_denominator(self):
        t = SideTally()
        t.add(grade(TICKET, Verdict(urgent_p=1.0, team="billing", frustration=3)), Verdict(urgent_p=1.0, team="billing", frustration=3), 0.01)
        t.add(grade(TICKET, Verdict(error="rate_limited")), Verdict(error="rate_limited"), None)
        s = t.summary()
        assert s["attempted"] == 2 and s["scored"] == 1 and s["errors"] == 1
        assert s["accuracy"]["urgent"] == 1.0
        assert s["error_kinds"] == {"rate_limited": 1}

    def test_unknown_cost_poisons_total(self):
        t = SideTally()
        v = Verdict(urgent_p=1.0, team="billing", frustration=3)
        t.add(grade(TICKET, v), v, None)
        assert t.summary()["total_cost_usd"] is None

    def test_winner_semantics(self):
        fast = SideTally()
        slow = SideTally()
        v_ok = Verdict(urgent_p=1.0, team="billing", frustration=3, latency_ms=10)
        v_slow = Verdict(urgent_p=0.0, team="sales", frustration=1, latency_ms=100)
        fast.add(grade(TICKET, v_ok), v_ok, 0.001)
        slow.add(grade(TICKET, v_slow), v_slow, 0.01)
        w = winner(fast, slow)
        assert w == {"speed": "jev", "cost": "jev", "accuracy": "jev"}

    def test_winner_one_sided_errors(self):
        ok = SideTally()
        broken = SideTally()
        v_ok = Verdict(urgent_p=1.0, team="billing", frustration=3, latency_ms=10)
        ok.add(grade(TICKET, v_ok), v_ok, 0.001)
        broken.add(grade(TICKET, Verdict(error="timeout")), Verdict(error="timeout"), None)
        w = winner(ok, broken)
        assert w["speed"] == "jev" and w["accuracy"] == "jev"

    def test_winner_tie(self):
        a, b = SideTally(), SideTally()
        v = Verdict(urgent_p=1.0, team="billing", frustration=3, latency_ms=10)
        for t_ in (a, b):
            t_.add(grade(TICKET, v), v, 0.001)
        assert winner(a, b) == {"speed": "tie", "cost": "tie", "accuracy": "tie"}
