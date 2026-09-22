"""App settings — everything comes from env vars / .env, nothing from clients."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Providers
    typesafe_api_key: str = ""
    jev_model: str = "jev-latest"
    anthropic_api_key: str = ""
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
    log_bodies: bool = False
    env: str = "dev"

    # Timeouts (seconds)
    per_attempt_timeout: float = 12.0
    per_ticket_timeout: float = 30.0

    @property
    def openai_compat_enabled(self) -> bool:
        return bool(self.openai_compat_base_url and self.openai_compat_model)

    @property
    def docs_enabled(self) -> bool:
        return self.env != "prod" and not self.race_token


@lru_cache
def get_settings() -> Settings:
    return Settings()
