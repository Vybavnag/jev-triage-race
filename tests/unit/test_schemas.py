"""Request-model validation.

Every rejection here happens inside pydantic, before any handler runs, so an
invalid request can never reach a provider or spend a rate-limit token.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import (
    MAX_CODE_CHARS,
    MAX_OWN_TICKETS,
    MAX_OWN_TICKETS_TOTAL_CHARS,
    MAX_QUESTION_CHARS,
    MAX_QUESTIONS,
    MAX_TICKET_CHARS,
    RaceItem,
    RaceRequest,
    ReviewRequest,
    Ticket,
)

OPP = {"kind": "anthropic", "model_id": "claude-sonnet-5"}


def race(**kw) -> RaceRequest:
    return RaceRequest(opponent=OPP, **kw)


def review(code: str = "x = 1", questions: list[str] | None = None, **kw) -> ReviewRequest:
    # None means "a valid default"; an explicit [] must reach the model as-is.
    if questions is None:
        questions = ["Is there a secret?"]
    return ReviewRequest(opponent=OPP, code=code, questions=questions, **kw)


def error_types(exc: pytest.ExceptionInfo) -> set[str]:
    return {e["type"] for e in exc.value.errors()}


# ── RaceItem / Ticket ────────────────────────────────────────────────────────


class TestRaceItem:
    def test_ticket_is_a_race_item_with_labels(self):
        t = Ticket(id="t1", text="x", urgent=True, team="billing", frustration=3)
        assert isinstance(t, RaceItem)

    def test_ticket_requires_labels(self):
        with pytest.raises(ValidationError):
            Ticket(id="t1", text="x")

    def test_race_item_carries_no_labels(self):
        item = RaceItem(id="p1", text="hello")
        assert item.id == "p1" and item.text == "hello"
        with pytest.raises(ValidationError):
            RaceItem(id="p1", text="hello", urgent=True)


# ── RaceRequest: source selection ────────────────────────────────────────────


class TestRaceRequestSource:
    def test_bundled_source(self):
        r = race(ticket_count=3)
        assert r.ticket_count == 3 and r.tickets is None

    def test_own_source(self):
        r = race(tickets=["a", "b"])
        assert r.ticket_count is None and r.tickets == ["a", "b"]

    @pytest.mark.parametrize("kw", [{}, {"ticket_count": 1, "tickets": ["a"]}])
    def test_exactly_one_source(self, kw):
        with pytest.raises(ValidationError, match="exactly one"):
            race(**kw)

    def test_ticket_count_at_least_one(self):
        with pytest.raises(ValidationError):
            race(ticket_count=0)

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            race(ticket_count=1, base_url="http://169.254.169.254")


# ── RaceRequest: own tickets ─────────────────────────────────────────────────


class TestOwnTickets:
    def test_stripped(self):
        assert race(tickets=["  hi  "]).tickets == ["hi"]

    @pytest.mark.parametrize("bad", [[], [""], ["   "], ["a", "\n\n"]])
    def test_empty_items_or_list_rejected(self, bad):
        with pytest.raises(ValidationError):
            race(tickets=bad)

    def test_per_ticket_char_cap(self):
        assert len(race(tickets=["x" * MAX_TICKET_CHARS]).tickets[0]) == MAX_TICKET_CHARS
        with pytest.raises(ValidationError):
            race(tickets=["x" * (MAX_TICKET_CHARS + 1)])

    def test_static_list_ceiling(self):
        assert len(race(tickets=["x"] * MAX_OWN_TICKETS).tickets) == MAX_OWN_TICKETS
        with pytest.raises(ValidationError) as exc:
            race(tickets=["x"] * (MAX_OWN_TICKETS + 1))
        assert "too_long" in error_types(exc)

    def test_total_chars_cap(self):
        full = ["x" * MAX_TICKET_CHARS] * (MAX_OWN_TICKETS_TOTAL_CHARS // MAX_TICKET_CHARS)
        remainder = MAX_OWN_TICKETS_TOTAL_CHARS - sum(len(t) for t in full)
        at_cap = full + (["x" * remainder] if remainder else [])
        assert sum(len(t) for t in race(tickets=at_cap).tickets) == MAX_OWN_TICKETS_TOTAL_CHARS
        with pytest.raises(ValidationError, match="in total"):
            race(tickets=at_cap + ["x"])

    def test_tabs_and_newlines_allowed_inside_a_ticket(self):
        assert race(tickets=["a\tb\nc\r\nd"]).tickets == ["a\tb\nc\r\nd"]

    @pytest.mark.parametrize("ch", ["\x00", "\x07", "\x1b", "\x7f", "\x85"])
    def test_other_control_characters_rejected(self, ch):
        with pytest.raises(ValidationError, match="control"):
            race(tickets=[f"a{ch}b"])

    def test_lone_surrogate_rejected(self):
        # json.loads accepts "\ud800"; encoding it to UTF-8 would fail inside
        # an adapter after the rate-limit token was already spent. Pydantic's
        # constrained-string validation is what rejects it; this pins that.
        with pytest.raises(ValidationError, match="unicode"):
            race(tickets=["a\ud800b"])


# ── ReviewRequest: code ──────────────────────────────────────────────────────


class TestReviewCode:
    def test_bounds(self):
        assert len(review(code="x" * MAX_CODE_CHARS).code) == MAX_CODE_CHARS
        with pytest.raises(ValidationError):
            review(code="x" * (MAX_CODE_CHARS + 1))
        with pytest.raises(ValidationError):
            review(code="")

    def test_whitespace_only_rejected(self):
        with pytest.raises(ValidationError, match="empty"):
            review(code="   \n\t ")

    def test_code_is_not_stripped_because_indentation_matters(self):
        assert review(code="  def f():\n    pass\n").code == "  def f():\n    pass\n"

    def test_tabs_and_newlines_allowed(self):
        assert review(code="a\tb\r\nc\n").code == "a\tb\r\nc\n"

    @pytest.mark.parametrize("ch", ["\x00", "\x1b", "\x7f"])
    def test_other_control_characters_rejected(self, ch):
        with pytest.raises(ValidationError, match="control"):
            review(code=f"a{ch}b")

    def test_lone_surrogate_rejected(self):
        with pytest.raises(ValidationError, match="unicode"):
            review(code="a\ud800b")

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            review(model="gpt-4")


# ── ReviewRequest: questions ─────────────────────────────────────────────────


def distinct(n: int) -> list[str]:
    return [f"Is there problem number {i}?" for i in range(n)]


class TestReviewQuestions:
    def test_count_bounds(self):
        assert len(review(questions=distinct(MAX_QUESTIONS)).questions) == MAX_QUESTIONS
        with pytest.raises(ValidationError):
            review(questions=[])
        with pytest.raises(ValidationError) as exc:
            review(questions=distinct(MAX_QUESTIONS + 1))
        assert "too_long" in error_types(exc)

    def test_count_is_checked_on_the_raw_list_before_duplicates(self):
        # 9 distinct + 2 duplicates = 11 items: the length error wins, so a
        # client can never sneak past the cap by padding with repeats.
        qs = distinct(MAX_QUESTIONS - 1) + ["Is it safe?", "Is it safe?"]
        with pytest.raises(ValidationError) as exc:
            review(questions=qs)
        assert error_types(exc) == {"too_long"}

    def test_duplicates_under_the_cap_are_rejected_not_deduped(self):
        with pytest.raises(ValidationError, match="duplicate"):
            review(questions=["Is it safe?", " is  IT safe? ", "Other one?"])

    def test_stripped_and_length_bounds(self):
        assert review(questions=["  abc  "]).questions == ["abc"]
        assert len(review(questions=["q" * MAX_QUESTION_CHARS]).questions[0]) == MAX_QUESTION_CHARS
        with pytest.raises(ValidationError):
            review(questions=["ab"])
        with pytest.raises(ValidationError):
            review(questions=["   ab   "])
        with pytest.raises(ValidationError):
            review(questions=["q" * (MAX_QUESTION_CHARS + 1)])

    @pytest.mark.parametrize(
        "ch",
        ["\n", "\t", "\x7f", "\x85", " ", " ", "​", "‮", "﻿"],
    )
    def test_line_breaks_controls_and_invisible_characters_rejected(self, ch):
        with pytest.raises(ValidationError, match="single line"):
            review(questions=[f"Is this{ch}safe?"])

    def test_lone_surrogate_rejected(self):
        with pytest.raises(ValidationError, match="unicode"):
            review(questions=["Is this \ud800 safe?"])

    def test_non_identifier_text_is_fine_because_ids_are_synthetic(self):
        # The text below is only data under test: nothing evaluates it.
        q = 'Does `eval(input())` run? — ünïcode "quoted"'
        assert review(questions=[q]).questions == [q]
