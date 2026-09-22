from app.security import (
    TokenBucket,
    check_race_token,
    client_ip,
    key_fingerprint,
    register_secret,
    scrub,
)
from tests.conftest import SENTINEL_KEY


class TestScrub:
    def test_key_patterns_redacted(self):
        assert "sk-ant" not in scrub("here is sk-ant-abcdefghijklmnopqrstuv123")
        assert "<redacted>" in scrub("Bearer abcdefghijklmnopqrstuvwx")

    def test_registered_secret_redacted_verbatim(self):
        register_secret("totally-custom-secret-value-42")
        assert "custom-secret" not in scrub("x totally-custom-secret-value-42 y")

    def test_nested_structures(self):
        blob = {
            "a": [{"api_key": "whatever"}, "text sk-ant-aaaaaaaaaaaaaaaaaaaaaaaa"],
            "Authorization": "Bearer abc",
        }
        out = scrub(blob)
        assert out["a"][0]["api_key"] == "<redacted>"
        assert "sk-ant" not in out["a"][1]
        assert out["Authorization"] == "<redacted>"

    def test_idempotent(self):
        blob = {"api_key": "x", "note": "sk-ant-aaaaaaaaaaaaaaaaaaaaaaaa"}
        assert scrub(scrub(blob)) == scrub(blob)

    def test_non_string_passthrough(self):
        assert scrub({"n": 3, "b": True, "x": None}) == {"n": 3, "b": True, "x": None}

    def test_int_keyed_dicts_survive(self):
        # Regression: Jev score answers carry int-keyed legend/probabilities
        # maps. Assuming str keys made every real Jev response look malformed.
        blob = {"legend": {0: "calm", 4: "furious"}, "probabilities": {0: 0.2, 1: 0.8}}
        assert scrub(blob) == blob

    def test_sentinel_key(self):
        register_secret(SENTINEL_KEY)
        assert SENTINEL_KEY not in str(scrub({"body": f"error with {SENTINEL_KEY}"}))


class TestRaceToken:
    def test_open_when_unconfigured(self):
        assert check_race_token("", None) is True

    def test_missing_and_wrong(self):
        assert check_race_token("secret", None) is False
        assert check_race_token("secret", "nope") is False

    def test_match(self):
        assert check_race_token("secret", "secret") is True


class TestTokenBucket:
    def test_burst_then_block_then_refill(self):
        now = [0.0]
        b = TokenBucket(capacity=1, refill_per_sec=1 / 10, clock=lambda: now[0])
        assert b.allow("ip1") is True
        assert b.allow("ip1") is False
        now[0] = 10.0
        assert b.allow("ip1") is True

    def test_keys_independent(self):
        b = TokenBucket(capacity=1, refill_per_sec=0.0)
        assert b.allow("a") is True
        assert b.allow("b") is True
        assert b.allow("a") is False


class TestClientIP:
    def test_socket_ip_when_proxy_untrusted(self):
        assert client_ip(("1.2.3.4", 5), "9.9.9.9", trust_proxy=False) == "1.2.3.4"

    def test_xff_leftmost_when_trusted(self):
        assert client_ip(("1.2.3.4", 5), "9.9.9.9, 8.8.8.8", trust_proxy=True) == "9.9.9.9"

    def test_no_client(self):
        assert client_ip(None, None, False) == "unknown"


def test_key_fingerprint_stable_and_short():
    assert key_fingerprint("abc") == key_fingerprint("abc")
    assert len(key_fingerprint("abc")) == 8
    assert key_fingerprint("") == "unset"
