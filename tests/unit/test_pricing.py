import pytest

from app.pricing import anthropic_cost, jev_cost, openai_compat_cost
from app.schemas import Usage

FULL = Usage(input_tokens=1_000_000, output_tokens=1_000_000)


def test_anthropic_known_models_exact():
    assert anthropic_cost("claude-opus-5", FULL) == pytest.approx(30.0)
    assert anthropic_cost("claude-sonnet-5", FULL) == pytest.approx(18.0)
    assert anthropic_cost("claude-haiku-4-5", FULL) == pytest.approx(6.0)


def test_unknown_model_is_none_not_zero():
    assert anthropic_cost("claude-nonexistent", FULL) is None


def test_missing_usage_is_none_not_zero():
    assert anthropic_cost("claude-opus-5", Usage()) is None
    assert jev_cost(Usage(input_tokens=100)) is None


def test_jev_output_free():
    assert jev_cost(Usage(input_tokens=1_000_000, output_tokens=999_999)) == pytest.approx(0.042)


def test_zero_tokens():
    assert jev_cost(Usage(input_tokens=0, output_tokens=0)) == 0.0


def test_openai_compat_env_prices():
    assert openai_compat_cost(FULL, 0.5, 1.5) == pytest.approx(2.0)
    assert openai_compat_cost(FULL, None, 1.5) is None
    assert openai_compat_cost(FULL, 0.5, None) is None


def test_small_usage_scales():
    u = Usage(input_tokens=1500, output_tokens=300)
    assert anthropic_cost("claude-haiku-4-5", u) == pytest.approx((1500 * 1 + 300 * 5) / 1e6)
