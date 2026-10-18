from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

_WEAK_SECRETS = {
    "",
    "change-me",
    "change-me-please",
    "local-admin-change-me",
    "admin",
    "password",
    "secret",
}


class Settings(BaseSettings):
    environment: str = "development"
    bot_token: str = ""
    bot_username: str = ""
    database_url: str = "postgresql+asyncpg://love:love@localhost:5432/anonymous_love"
    redis_url: str = ""
    admin_jwt_secret: str = "change-me-please"
    identity_pepper: str = "change-me-please"
    admin_bootstrap_username: str = ""
    admin_bootstrap_password: str = ""
    use_webhook: bool = False
    webhook_base_url: str = ""
    webhook_secret: str = ""
    payment_mode: str = "disabled"
    crypto_pay_token: str = ""
    crypto_pay_api: str = "https://pay.crypt.bot/api"
    crypto_asset: str = "USDT"
    star_usd_cents: int = 2
    log_level: str = "INFO"
    log_json: bool = False
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:5173,http://localhost:5174,http://localhost:8080"
    miniapp_url: str = ""
    verification_provider_url: str = ""
    verification_provider_key: str = ""
    verification_provider_secret: str = ""
    access_token_hours: int = 12
    miniapp_token_hours: int = 12
    auth_date_max_age_sec: int = 86400

    model_config = SettingsConfigDict(
        env_file=".env" if os.getenv("ENVIRONMENT") != "test" else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url.strip())

    @property
    def cors_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def provider_configured(self) -> bool:
        return bool(self.verification_provider_url and self.verification_provider_key and self.verification_provider_secret)


def validate_runtime(settings: Settings) -> None:
    if settings.environment != "production":
        return
    problems: list[str] = []
    if settings.admin_jwt_secret in _WEAK_SECRETS or len(settings.admin_jwt_secret) < 24:
        problems.append("ADMIN_JWT_SECRET")
    if settings.identity_pepper in _WEAK_SECRETS or len(settings.identity_pepper) < 16:
        problems.append("IDENTITY_PEPPER")
    if settings.payment_mode == "test":
        problems.append("PAYMENT_MODE=test is refused in production")
    if "love:love@" in settings.database_url:
        problems.append("DATABASE_URL still uses the local sample password")
    if settings.admin_bootstrap_password and (
        settings.admin_bootstrap_password in _WEAK_SECRETS or len(settings.admin_bootstrap_password) < 10
    ):
        problems.append("ADMIN_BOOTSTRAP_PASSWORD")
    if settings.use_webhook and not settings.webhook_secret:
        problems.append("WEBHOOK_SECRET")
    if problems:
        raise RuntimeError("Unsafe production configuration: " + ", ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
