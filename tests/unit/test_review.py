"""The code-review checklist: prompt shape, the fixed per-N output model, and
the JSON schema both LLM adapters send."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.review import (
    DEFAULT_QUESTIONS,
    REVIEW_MAX_TOKENS,
    SYSTEM_PROMPT,
    build_prompt,
    question_ids,
    redacted_prompt,
    review_json_schema,
    review_model,
    yes_from_p,
)
from app.schemas import MAX_QUESTIONS, ReviewRequest

OPP = {"kind": "anthropic", "model_id": "claude-sonnet-5"}


def test_default_questions_are_valid_review_input():
    req = ReviewRequest(code="x = 1", questions=DEFAULT_QUESTIONS, opponent=OPP)
    assert 1 <= len(req.questions) <= MAX_QUESTIONS
    assert req.questions == DEFAULT_QUESTIONS  # already stripped, no duplicates


def test_question_ids_are_one_based():
    assert question_ids(3) == ["q1", "q2", "q3"]


def test_review_max_tokens_leaves_room_for_thinking():
    assert REVIEW_MAX_TOKENS >= 2048


class TestPrompt:
    def test_code_comes_first_then_numbered_questions_then_the_instruction(self):
        p = build_prompt("x = 1", ["Is it safe?", "Bad call?"])
        assert "x = 1" in p
        assert p.index("<code>") < p.index("1. Is it safe?") < p.index("2. Bad call?")
        assert p.index("2. Bad call?") < p.index("q1..q2")
        assert "data" in p  # the code is data to inspect, not instructions

    def test_redacted_prompt_omits_the_code_but_keeps_the_questions(self):
        code = "password = 'hunter2'"
        r = redacted_prompt(code, ["Any secret?"])
        assert "hunter2" not in r
        assert f"<code omitted: {len(code)} chars>" in r
        assert "1. Any secret?" in r

    def test_system_prompt_frames_code_as_data(self):
        assert "data" in SYSTEM_PROMPT


class TestModel:
    def test_fields_are_strict_bools_with_no_descriptions(self):
        model = review_model(3)
        assert list(model.model_fields) == ["q1", "q2", "q3"]
        assert all(f.description is None for f in model.model_fields.values())
        assert model.model_config["extra"] == "forbid"
        parsed = model(q1=True, q2=False, q3=True)
        assert (parsed.q1, parsed.q2, parsed.q3) == (True, False, True)

    @pytest.mark.parametrize(
        "payload",
        [
            {"q1": 1, "q2": False},  # 1 is not a bool
            {"q1": "true", "q2": False},
            {"q1": True},  # missing
            {"q1": True, "q2": False, "q3": True},  # extra
        ],
    )
    def test_anything_but_exactly_n_bools_is_rejected(self, payload):
        with pytest.raises(ValidationError):
            review_model(2)(**payload)

    def test_same_n_reuses_the_class_so_the_schema_stays_cacheable(self):
        assert review_model(2) is review_model(2)
        assert review_model(1) is not review_model(2)

    def test_json_schema_is_strict_and_matches_the_ids(self):
        s = review_json_schema(2)
        assert s["type"] == "object"
        assert list(s["properties"]) == ["q1", "q2"]
        assert all(prop == {"type": "boolean"} for prop in s["properties"].values())
        assert s["required"] == ["q1", "q2"]
        assert s["additionalProperties"] is False


@pytest.mark.parametrize(("p", "expected"), [(0.4999, False), (0.5, True), (1.0, True)])
def test_yes_threshold_is_inclusive(p, expected):
    assert yes_from_p(p) is expected
