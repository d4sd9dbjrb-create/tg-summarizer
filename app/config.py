from __future__ import annotations

from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram API (один на весь сервис)
    tg_api_id: int = Field(alias="TG_API_ID")
    tg_api_hash: str = Field(alias="TG_API_HASH")

    # Control bot
    control_bot_token: str = Field(alias="CONTROL_BOT_TOKEN")
    control_bot_username: str = Field(alias="CONTROL_BOT_USERNAME")
    admin_user_id: int = Field(alias="ADMIN_USER_ID")

    # Beta access
    invite_code: str = Field(alias="INVITE_CODE")

    # Security
    session_encryption_key: str = Field(alias="SESSION_ENCRYPTION_KEY")
    web_secret_key: str = Field(alias="WEB_SECRET_KEY")

    # LLM
    llm_provider: str = Field(default="deepseek", alias="LLM_PROVIDER")
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_model: str = Field(default="deepseek-v4-pro", alias="DEEPSEEK_MODEL")
    deepseek_model_fast: str = Field(default="deepseek-v4-flash", alias="DEEPSEEK_MODEL_FAST")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.0-flash", alias="GEMINI_MODEL")

    # DB
    postgres_user: str = Field(default="tg", alias="POSTGRES_USER")
    postgres_password: str = Field(default="tg", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="tg", alias="POSTGRES_DB")
    postgres_host: str = Field(default="db", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")

    # Web
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=8000, alias="WEB_PORT")
    public_base_url: str = Field(default="http://localhost:8000", alias="PUBLIC_BASE_URL")

    # Behaviour
    timezone: str = Field(default="Europe/Moscow", alias="TIMEZONE")
    daily_digest_hour: int = Field(default=9, alias="DAILY_DIGEST_HOUR")
    ingest_mode: str = Field(default="all", alias="INGEST_MODE")  # all | whitelist
    message_retention_days: int = Field(default=90, alias="MESSAGE_RETENTION_DAYS")

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
