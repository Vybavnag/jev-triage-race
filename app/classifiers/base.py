"""Classifier protocols and the shared adapter core.

Every adapter has one private `_call` that does the network call, maps the
provider's errors onto the shared error kinds, and reports usage and
latency. The public `classify` and `review` methods build the request, hand
it to `_call`, and do only the shape-specific parsing. Anything that fails
to parse is a `malformed` verdict, never an exception.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

from app.schemas import MAX_RAW_BLOB_BYTES, ReviewVerdict, Usage, Verdict, VerdictBase
from app.security import scrub

# Error kinds every adapter maps into (VerdictBase.error)
RATE_LIMITED = "rate_limited"
REFUSAL = "refusal"
TIMEOUT = "timeout"
UPSTREAM_ERROR = "upstream_error"
MALFORMED = "malformed"
INVALID_KEY = "invalid_key"  # the visitor's own key was rejected by the provider

REDACTED_AUTH_HEADERS = {"authorization": "<redacted>"}

T = TypeVar("T")
V = TypeVar("V", bound=VerdictBase)
M = TypeVar("M", bound=BaseModel)


class Classifier(Protocol):
    name: str

    async def classify(self, text: str) -> Verdict: ...


class Reviewer(Protocol):
    name: str

    async def review(
        self, code: str, questions: Sequence[str], *, timeout: float | None = None
    ) -> ReviewVerdict: ...


@dataclass(frozen=True)
class CallResult(Generic[T]):
    """What one provider call produced, before any shape-specific parsing.
    `payload` is None exactly when `error` is set."""

    payload: T | None
    error: str | None
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    meta: dict = field(default_factory=dict)  # provider-reported model, stop_reason


def failed(
    cls: type[V], call: CallResult, raw_request: dict | None, kind: str | None = None
) -> V:
    """An errored verdict of the right shape, carrying whatever the call
    already learned (usage, latency, the provider's model and stop reason)
    and the redacted request, so a refusal or a truncation can be diagnosed
    from the inspector. `meta` never holds text, so it is safe to echo."""
    raw_response = (
        finalize_raw({**call.meta, "usage": call.usage.model_dump()}) if call.meta else None
    )
    return cls(
        error=kind or call.error,
        usage=call.usage,
        latency_ms=call.latency_ms,
        raw_request=raw_request,
        raw_response=raw_response,
    )


def validate_json(model: type[M], text: str) -> M | None:
    """Strict parse into `model`; None on any failure. Truncated bodies, extra
    keys, and wrong types all land here. pydantic.ValidationError is a
    ValueError, as is json's decode error."""
    try:
        return model.model_validate_json(text)
    except ValueError:
        return None


def finalize_raw(blob: dict | None) -> dict | None:
    """Scrub key material and cap the blob size so a pathological upstream
    response can't blow up an SSE frame or the inspector."""
    if blob is None:
        return None
    cleaned = scrub(blob)
    encoded = json.dumps(cleaned, default=str)
    if len(encoded.encode()) > MAX_RAW_BLOB_BYTES:
        return {"truncated": True, "preview": encoded[: MAX_RAW_BLOB_BYTES // 2]}
    return cleaned


class Stopwatch:
    def __enter__(self) -> "Stopwatch":
        self._start = time.perf_counter()
        self.ms = 0.0
        return self

    def __exit__(self, *exc) -> None:
        self.ms = (time.perf_counter() - self._start) * 1000.0
