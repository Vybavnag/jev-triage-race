import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import Settings
from app.deps import (
    ReviewSlots,
    VisitorKeys,
    configure_limits,
    race_bucket,
    review_bucket,
    review_slots,
    visitor_keys,
)
from tests.conftest import SENTINEL_ANTHROPIC, SENTINEL_KEY


def request_with(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode("latin-1")) for k, v in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw, "query_string": b""})


class TestReviewSlots:
    def test_refuses_beyond_the_limit_without_queueing(self):
        slots = ReviewSlots(limit=2)
        assert slots.try_acquire() is True
        assert slots.try_acquire() is True
        assert slots.try_acquire() is False
        assert slots.in_flight == 2

    def test_release_frees_a_slot(self):
        slots = ReviewSlots(limit=1)
        assert slots.try_acquire() is True
        slots.release()
        assert slots.in_flight == 0
        assert slots.try_acquire() is True

    def test_release_never_goes_negative(self):
        slots = ReviewSlots(limit=1)
        slots.release()
        assert slots.in_flight == 0

    def test_reset_clears_everything(self):
        slots = ReviewSlots(limit=1)
        slots.try_acquire()
        slots.reset()
        assert slots.try_acquire() is True

    def test_limit_can_be_raised_and_lowered(self):
        slots = ReviewSlots(limit=1)
        slots.limit = 3
        assert [slots.try_acquire() for _ in range(4)] == [True, True, True, False]


class TestVisitorKeys:
    """Provider keys arrive per request in headers. They are checked for shape
    only; the provider decides whether they are real."""

    def test_both_keys_read_and_trimmed(self):
        keys = visitor_keys(request_with({"X-TypeSafe-Key": f" {SENTINEL_KEY} ", "X-Anthropic-Key": SENTINEL_ANTHROPIC}))
        assert keys == VisitorKeys(typesafe=SENTINEL_KEY, anthropic=SENTINEL_ANTHROPIC)

    def test_anthropic_key_is_optional(self):
        keys = visitor_keys(request_with({"X-TypeSafe-Key": SENTINEL_KEY}))
        assert keys.anthropic is None

    @pytest.mark.parametrize("headers", [{}, {"X-TypeSafe-Key": ""}, {"X-TypeSafe-Key": "   "}])
    def test_missing_typesafe_key_is_a_400_naming_the_header(self, headers):
        with pytest.raises(HTTPException) as exc:
            visitor_keys(request_with(headers))
        assert exc.value.status_code == 400
        assert "X-TypeSafe-Key" in exc.value.detail

    @pytest.mark.parametrize(
        "bad",
        [
            "short",
            "has space inside" + "x" * 10,
            "x" * 513,
            "tab\tinside" + "x" * 10,
            # Headers decode as latin-1, so an accented character passes
            # isprintable(); both SDKs refuse it later, one with a 500.
            "abc\xe9defghij",
        ],
    )
    def test_malformed_keys_are_rejected_without_being_echoed(self, bad):
        with pytest.raises(HTTPException) as exc:
            visitor_keys(request_with({"X-TypeSafe-Key": bad}))
        assert exc.value.status_code == 400
        assert bad not in exc.value.detail
        with pytest.raises(HTTPException):
            visitor_keys(request_with({"X-TypeSafe-Key": SENTINEL_KEY, "X-Anthropic-Key": bad}))


class TestConfigureLimits:
    def test_bucket_sizes_and_slots_come_from_settings(self):
        settings = Settings(
            race_burst=7, race_per_minute=30, review_burst=2, review_per_minute=6, review_in_flight=4, _env_file=None
        )
        configure_limits(settings)
        try:
            assert race_bucket.capacity == 7 and race_bucket.refill_per_sec == pytest.approx(0.5)
            assert review_bucket.capacity == 2 and review_bucket.refill_per_sec == pytest.approx(0.1)
            assert review_slots.limit == 4
        finally:
            configure_limits(Settings(_env_file=None))  # back to the defaults for other tests

    def test_defaults_match_the_documented_public_mode(self):
        configure_limits(Settings(_env_file=None))
        assert race_bucket.capacity == 3 and race_bucket.refill_per_sec == pytest.approx(1 / 60)
        assert review_bucket.capacity == 5 and review_bucket.refill_per_sec == pytest.approx(2 / 60)
        assert review_slots.limit == 2
