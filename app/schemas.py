"""Pydantic models shared across the app.

Everything uses extra="forbid" so unexpected client fields are a 422, never
silently ignored.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TEAMS = ("billing", "technical", "account", "sales")
FRUSTRATION_LEVELS = 5  # rubric levels 1..5 (Jev returns 0-indexed 0.0..4.0)

MAX_PLAYGROUND_CHARS = 4_000
MAX_RAW_BLOB_BYTES = 32_768  # cap raw_request/raw_response blobs in responses


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OpponentKind(StrEnum):
    anthropic = "anthropic"
    openai_compat = "openai_compat"


class Ticket(StrictModel):
    id: str
    text: str
    urgent: bool
    team: Literal["billing", "technical", "account", "sales"]
    frustration: int = Field(ge=1, le=5)


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


class Verdict(StrictModel):
    """Normalized output of one classifier call for one text.

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
    usage: Usage = Usage()
    latency_ms: float = 0.0
    raw_request: dict | None = None
    raw_response: dict | None = None
    error: str | None = None  # kind: rate_limited | refusal | timeout | upstream_error | malformed


class Opponent(StrictModel):
    kind: OpponentKind
    model_id: str


class RaceRequest(StrictModel):
    opponent: Opponent
    ticket_count: int = Field(ge=1)


class PlaygroundRequest(StrictModel):
    text: str = Field(min_length=1, max_length=MAX_PLAYGROUND_CHARS)
    opponent: Opponent


class Correct(StrictModel):
    urgent: bool | None = None
    team: bool | None = None
    frustration: bool | None = None


class SideResult(StrictModel):
    provider: str
    verdict: Verdict
    cost_usd: float | None = None  # None => unknown (missing usage or price)


class PlaygroundResponse(StrictModel):
    jev: SideResult
    llm: SideResult
