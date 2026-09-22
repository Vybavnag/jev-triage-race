"""Security helpers: response scrubbing, rate limiting, race-token check."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# Provider key shapes. Applied to every string that leaves the server as
# defense-in-depth — the primary control is never putting keys into
# responses in the first place.
_KEY_PATTERNS = [
    re.compile(r"sk-ant-[\w-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"Bearer\s+[\w.\-]{16,}"),
]

_SECRET_BODY_KEYS = {"api_key", "apikey", "authorization", "x-api-key", "token", "secret"}

REDACTED = "<redacted>"


def register_secret(value: str) -> None:
    """Register a live secret (e.g. an env key) so scrub() removes it verbatim."""
    if value and len(value) >= 12:
        _live_secrets.add(value)


_live_secrets: set[str] = set()


def _scrub_str(s: str) -> str:
    for secret in _live_secrets:
        s = s.replace(secret, REDACTED)
    for pat in _KEY_PATTERNS:
        s = pat.sub(REDACTED, s)
    return s


def scrub(value: Any) -> Any:
    """Recursively redact key material from any JSON-shaped value. Idempotent."""
    if isinstance(value, str):
        return _scrub_str(value)
    if isinstance(value, dict):
        # Keys are not always strings (Jev score answers use int-keyed legend
        # and probabilities maps), so never assume str methods on a key.
        return {
            k: REDACTED
            if isinstance(k, str) and k.lower() in _SECRET_BODY_KEYS
            else scrub(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


def key_fingerprint(key: str) -> str:
    """Safe-to-log identifier for a key."""
    return hashlib.sha256(key.encode()).hexdigest()[:8] if key else "unset"


def check_race_token(configured: str, presented: str | None) -> bool:
    """Constant-time token check. When no token is configured, access is open."""
    if not configured:
        return True
    if not presented:
        return False
    return secrets.compare_digest(configured, presented)


@dataclass
class TokenBucket:
    """Per-key token bucket. Clock is injectable for tests."""

    capacity: float
    refill_per_sec: float
    clock: Callable[[], float] = time.monotonic
    _state: dict[str, tuple[float, float]] = field(default_factory=dict)  # key -> (tokens, ts)

    def allow(self, key: str) -> bool:
        now = self.clock()
        tokens, ts = self._state.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - ts) * self.refill_per_sec)
        if tokens >= 1.0:
            self._state[key] = (tokens - 1.0, now)
            return True
        self._state[key] = (tokens, now)
        return False

    def reset(self) -> None:
        self._state.clear()


def client_ip(scope_client: tuple[str, int] | None, xff: str | None, trust_proxy: bool) -> str:
    """Rate-limit key: socket IP unless the operator opted into trusting the
    proxy's X-Forwarded-For (leftmost entry)."""
    if trust_proxy and xff:
        return xff.split(",")[0].strip()
    return scope_client[0] if scope_client else "unknown"
