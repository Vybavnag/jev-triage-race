"""App settings — everything comes from env vars / .env, nothing from clients.

Provider keys are deliberately NOT here. Visitors enter their own TypeSafe
and Anthropic keys in the page, and each request carries them in headers;
the server uses them for that run and keeps nothing. The one exception is
the optional OpenAI-compatible endpoint, which is operator-configured
because it carries a base URL the server will connect to.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Providers
    jev_model: str = "jev-latest"
    openai_compat_base_url: str = ""
    openai_compat_api_key: str = ""
    openai_compat_model: str = ""
    openai_compat_price_in: float | None = None
    openai_compat_price_out: float | None = None

    # Guardrails
    race_token: str = ""
    race_max_items: int = 25
    jev_concurrency: int = 8
    llm_concurrency: int = 4
    trust_proxy: bool = False
    env: str = "dev"

    # Rate limiting (public mode; a RACE_TOKEN holder is never limited).
    # Visitors spend their own provider keys, so these protect this server's
    # compute, not a wallet. Set RATE_LIMITING=false on a local clone.
    rate_limiting: bool = True
    race_burst: int = 3
    race_per_minute: float = 1.0
    review_burst: int = 5
    review_per_minute: float = 2.0
    review_in_flight: int = 2

    # Timeouts (seconds)
    per_attempt_timeout: float = 12.0
    per_ticket_timeout: float = 30.0
    # A review reads up to 16k chars of code with thinking on, which needs
    # more than a ticket: per attempt, then the outer bound for one side
    # (the SDK retries once, so the outer bound covers two attempts).
    review_attempt_timeout: float = 25.0
    review_timeout: float = 60.0

    @property
    def openai_compat_enabled(self) -> bool:
        return bool(self.openai_compat_base_url and self.openai_compat_model)

    @property
    def docs_enabled(self) -> bool:
        return self.env != "prod" and not self.race_token


@lru_cache
def get_settings() -> Settings:
    return Settings()
