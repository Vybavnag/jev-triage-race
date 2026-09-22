"""Pydantic models shared across the app.

Everything uses extra="forbid" so unexpected client fields are a 422, never
silently ignored. Every piece of text a client can send is validated here, so
a bad request is rejected before any handler runs, before a rate-limit token
is spent, and before a provider is called.
"""

from __future__ import annotations

import unicodedata
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

TEAMS = ("billing", "technical", "account", "sales")
FRUSTRATION_LEVELS = 5  # rubric levels 1..5 (Jev returns 0-indexed 0.0..4.0)

MAX_RAW_BLOB_BYTES = 32_768  # cap raw_request/raw_response blobs in responses

# Own (pasted) tickets. The static list ceiling bounds validation work before
# the handler applies the operator's race_max_items.
MAX_TICKET_CHARS = 4_000
MAX_OWN_TICKETS = 100
MAX_OWN_TICKETS_TOTAL_CHARS = 50_000

# Code review
MAX_CODE_CHARS = 16_000
MAX_QUESTIONS = 10
MAX_QUESTION_CHARS = 200


# ── Text validators ──────────────────────────────────────────────────────────

_ALLOWED_CONTROLS = frozenset("\t\n\r")
_LINE_BREAKING_OR_INVISIBLE = frozenset({"Cc", "Cf", "Zl", "Zp"})

# Lone UTF-16 surrogates need no validator of their own: json.loads accepts
# them, but pydantic's constrained-string validation (any StringConstraints or
# length Field below) rejects them as `string_unicode`. The schema tests pin
# that, because encoding one later inside an adapter would raise after the
# rate-limit token was already spent.


def no_controls(s: str) -> str:
    """Multi-line text: tabs and newlines are content, other controls are not."""
    if any(unicodedata.category(ch) == "Cc" and ch not in _ALLOWED_CONTROLS for ch in s):
        raise ValueError("control characters are not allowed")
    return s


def single_line(s: str) -> str:
    """One printable line: no controls, no line or paragraph separators, and
    no invisible format characters (zero-width, bidi overrides, BOM) that
    could make the UI show something other than what was sent."""
    if any(unicodedata.category(ch) in _LINE_BREAKING_OR_INVISIBLE for ch in s):
        raise ValueError("must be a single line of printable text")
    return s


def not_blank(s: str) -> str:
    if not s.strip():
        raise ValueError("text is empty")
    return s


TicketText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TICKET_CHARS),
    AfterValidator(no_controls),
]

# Never stripped: indentation is part of the code under review.
CodeText = Annotated[
    str,
    Field(min_length=1, max_length=MAX_CODE_CHARS),
    AfterValidator(no_controls),
    AfterValidator(not_blank),
]

QuestionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=3, max_length=MAX_QUESTION_CHARS),
    AfterValidator(single_line),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpponentKind(StrEnum):
    anthropic = "anthropic"
    openai_compat = "openai_compat"


# ── Race items ───────────────────────────────────────────────────────────────


class RaceItem(StrictModel):
    """One text to classify. Own (pasted) tickets are plain RaceItems: timed
    and costed, never graded."""

    id: str
    text: str


class Ticket(RaceItem):
    """A bundled ticket: a RaceItem plus the labels it is graded against."""

    urgent: bool
    team: Literal["billing", "technical", "account", "sales"]
    frustration: int = Field(ge=1, le=5)


# ── Verdicts ─────────────────────────────────────────────────────────────────


class Usage(StrictModel):
    input_tokens: int | None = None
    output_tokens: int | None = None


class TeamOption(StrictModel):
    option: str
    p: float


class FrustrationLevel(StrictModel):
    level: int  # 1..5, already mapped off Jev's 0-indexed scale
    label: str
    p: float


class VerdictBase(StrictModel):
    """What every classifier call reports regardless of the question set."""

    usage: Usage = Usage()
    latency_ms: float = 0.0
    raw_request: dict | None = None
    raw_response: dict | None = None
    error: str | None = None  # kind: rate_limited | refusal | timeout | upstream_error | malformed


class Verdict(VerdictBase):
    """Normalized output of one triage call for one text.

    The distribution fields are the part an LLM cannot fill in: it asserts one
    value per field, so they stay None for the LLM adapters.
    """

    urgent_p: float | None = None  # probability urgent (Jev noul / LLM bool -> 0/1)
    team: str | None = None
    team_confidence: float | None = None
    team_distribution: list[TeamOption] | None = None
    frustration_distribution: list[FrustrationLevel] | None = None
    frustration_raw: float | None = None  # provider-native scale (Jev: 0-indexed float)
    frustration: int | None = None  # mapped 1..5


class ReviewAnswer(StrictModel):
    """One yes/no answer. Jev reports a probability; the LLM only a boolean."""

    id: str
    p: float | None = None
    yes: bool | None = None


class ReviewVerdict(VerdictBase):
    answers: list[ReviewAnswer] = Field(default_factory=list)


# ── Requests ─────────────────────────────────────────────────────────────────


class Opponent(StrictModel):
    kind: OpponentKind
    model_id: str


class RaceRequest(StrictModel):
    """Exactly one source: a count of bundled (labeled) tickets, or the
    caller's own unlabeled tickets."""

    opponent: Opponent
    ticket_count: int | None = Field(default=None, ge=1)
    tickets: list[TicketText] | None = Field(
        default=None, min_length=1, max_length=MAX_OWN_TICKETS
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> RaceRequest:
        if (self.ticket_count is None) == (self.tickets is None):
            raise ValueError("provide exactly one of ticket_count or tickets")
        if self.tickets is not None:
            total = sum(len(t) for t in self.tickets)
            if total > MAX_OWN_TICKETS_TOTAL_CHARS:
                raise ValueError(
                    f"tickets exceed {MAX_OWN_TICKETS_TOTAL_CHARS:,} characters in total"
                )
        return self


class ReviewRequest(StrictModel):
    code: CodeText
    questions: list[QuestionText] = Field(min_length=1, max_length=MAX_QUESTIONS)
    opponent: Opponent

    @field_validator("questions")
    @classmethod
    def _no_duplicates(cls, questions: list[str]) -> list[str]:
        # Rejected rather than de-duplicated: the server never silently changes
        # the request, so answer ids always line up with what the caller sent.
        seen: set[str] = set()
        for q in questions:
            key = " ".join(q.casefold().split())
            if key in seen:
                raise ValueError(f"duplicate question: {q!r}")
            seen.add(key)
        return questions


# ── Responses ────────────────────────────────────────────────────────────────


class ReviewSide(StrictModel):
    provider: str
    verdict: ReviewVerdict
    cost_usd: float | None = None  # None => unknown (missing usage or price)


class ReviewResponse(StrictModel):
    questions: list[str]  # index-aligned with each side's answers
    jev: ReviewSide
    llm: ReviewSide
