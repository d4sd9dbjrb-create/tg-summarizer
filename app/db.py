from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings

settings = get_settings()

engine = create_async_engine(settings.db_url, pool_pre_ping=True, echo=False)
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def init_db() -> None:
    from .models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await migrate_db()


_MIGRATIONS = [
    # accounts: privacy settings
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS analyze_dms BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS analyze_groups BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS analyze_channels BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS ignore_bots BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS ignore_archived BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS ignore_read_only BOOLEAN NOT NULL DEFAULT FALSE",
    # accounts: scan settings
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS scan_limit_per_chat INTEGER NOT NULL DEFAULT 300",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS scan_max_dialogs INTEGER NOT NULL DEFAULT 200",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS scan_include_dms BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS scan_include_groups BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS scan_include_channels BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS scan_skip_bots BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS scan_skip_archived BOOLEAN NOT NULL DEFAULT TRUE",
    # chats: ignore reason + priority
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS ignore_reason VARCHAR(32)",
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS is_priority BOOLEAN NOT NULL DEFAULT FALSE",
    # accounts: per-user LLM keys/models
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS user_deepseek_api_key_enc TEXT",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS user_deepseek_model VARCHAR(64)",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS user_gemini_api_key_enc TEXT",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS user_gemini_model VARCHAR(64)",
]


async def migrate_db() -> None:
    """Безопасно добавляет новые колонки (IF NOT EXISTS). Идемпотентна."""
    from sqlalchemy import text

    async with engine.begin() as conn:
        for sql in _MIGRATIONS:
            await conn.execute(text(sql))
