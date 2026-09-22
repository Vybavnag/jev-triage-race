"""Classifier protocol and shared adapter helpers."""

from __future__ import annotations

import json
import time
from typing import Protocol

from app.schemas import MAX_RAW_BLOB_BYTES
from app.security import scrub

# Error kinds every adapter maps into (Verdict.error)
RATE_LIMITED = "rate_limited"
REFUSAL = "refusal"
TIMEOUT = "timeout"
UPSTREAM_ERROR = "upstream_error"
MALFORMED = "malformed"

REDACTED_AUTH_HEADERS = {"authorization": "<redacted>"}


class Classifier(Protocol):
    name: str

    async def classify(self, text: str) -> "Verdict":  # noqa: F821 - forward ref
        ...


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
