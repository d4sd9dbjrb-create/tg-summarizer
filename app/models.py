from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
#  Multi-tenant: учётные записи сервиса
# ---------------------------------------------------------------------------

class Account(Base):
    """Один пользователь сервиса = один Telegram-аккаунт."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Привязка к Telegram
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    tg_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tg_first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tg_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Telethon StringSession, зашифрованный Fernet'ом
    encrypted_session: Mapped[str] = mapped_column(Text)

    # One-time token для связки веб->control bot (через deep-link /start <token>)
    link_token: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
    link_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Состояние
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | disabled | logged_out
    accepted_tos_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bot_linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Настройки (per-account)
    daily_digest_hour: Mapped[int] = mapped_column(SmallInteger, default=9)
    ingest_mode: Mapped[str] = mapped_column(String(16), default="all")  # all | whitelist
    auto_tag_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sentiment_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    llm_provider: Mapped[str] = mapped_column(String(16), default="deepseek")

    # Пользовательские ключи (зашифрованы Fernet) и переопределение модели.
    # Если NULL — используется серверный дефолт из settings.
    user_deepseek_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_deepseek_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_gemini_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_gemini_model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Настройки приватности
    analyze_dms: Mapped[bool] = mapped_column(Boolean, default=True)       # личные переписки
    analyze_groups: Mapped[bool] = mapped_column(Boolean, default=True)    # группы
    analyze_channels: Mapped[bool] = mapped_column(Boolean, default=True)  # каналы/новостные
    ignore_bots: Mapped[bool] = mapped_column(Boolean, default=True)       # не хранить сообщения от ботов
    ignore_archived: Mapped[bool] = mapped_column(Boolean, default=True)   # пропускать архивные чаты
    ignore_read_only: Mapped[bool] = mapped_column(Boolean, default=False)  # пропускать каналы где нет своих постов

    # Настройки сканирования (backfill)
    scan_limit_per_chat: Mapped[int] = mapped_column(Integer, default=300)    # сообщений на чат
    scan_max_dialogs: Mapped[int] = mapped_column(Integer, default=200)       # максимум чатов
    scan_include_dms: Mapped[bool] = mapped_column(Boolean, default=True)
    scan_include_groups: Mapped[bool] = mapped_column(Boolean, default=True)
    scan_include_channels: Mapped[bool] = mapped_column(Boolean, default=True)
    scan_skip_bots: Mapped[bool] = mapped_column(Boolean, default=True)
    scan_skip_archived: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Chat(Base):
    """Чат конкретного аккаунта (один и тот же tg_chat_id может быть у разных юзеров)."""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    tg_chat_id: Mapped[int] = mapped_column(BigInteger, index=True)

    title: Mapped[str] = mapped_column(String(512), default="")
    type: Mapped[str] = mapped_column(String(32), default="")  # user|group|channel
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_tracked: Mapped[bool] = mapped_column(Boolean, default=True)
    is_muted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_priority: Mapped[bool] = mapped_column(Boolean, default=False)
    ignore_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)  # manual|bot|dm|channel|group
    auto_tag: Mapped[str | None] = mapped_column(String(32), nullable=True)  # work|friends|news|spam|other
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    messages: Mapped[list["Message"]] = relationship(back_populates="chat", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("account_id", "tg_chat_id", name="uq_account_chat"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int] = mapped_column(Integer, ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    tg_message_id: Mapped[int] = mapped_column(BigInteger)

    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    reply_to_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_outgoing: Mapped[bool] = mapped_column(Boolean, default=False)
    mentions_me: Mapped[bool] = mapped_column(Boolean, default=False)
    forward_from: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Enrichment поля (заполняются hourly worker'ом)
    sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)  # -1..+1
    auto_tag: Mapped[str | None] = mapped_column(String(32), nullable=True)
    importance_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)  # 0..10
    is_question_to_me: Mapped[bool] = mapped_column(Boolean, default=False)
    is_action_item: Mapped[bool] = mapped_column(Boolean, default=False)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    chat: Mapped[Chat] = relationship(back_populates="messages")

    __table_args__ = (
        UniqueConstraint("account_id", "chat_id", "tg_message_id", name="uq_account_chat_msg"),
        Index("ix_messages_acc_date", "account_id", "date"),
        Index("ix_messages_acc_enriched", "account_id", "enriched_at"),
    )


class Summary(Base):
    __tablename__ = "summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("chats.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))  # daily|weekly|on_demand|catchup|important
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Keyword(Base):
    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    word: Mapped[str] = mapped_column(String(255), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ContactProfile(Base):
    """LLM-генерируемая карточка контакта (этап B)."""

    __tablename__ = "contact_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    contact_user_id: Mapped[int] = mapped_column(BigInteger)
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    narrative: Mapped[str] = mapped_column(Text, default="")
    talking_points: Mapped[str] = mapped_column(Text, default="")
    avoid_topics: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("account_id", "contact_user_id", name="uq_account_contact"),
    )


class Link(Base):
    """Ссылки из сообщений для reading list (этап B)."""

    __tablename__ = "links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[int] = mapped_column(Integer, ForeignKey("messages.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(String(2048))
    domain: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AskSession(Base):
    """Диалог пользователя с ИИ-ассистентом по своим чатам."""

    __tablename__ = "ask_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255), default="Новый диалог")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["AskMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="AskMessage.id",
    )


class AskMessage(Base):
    """Одна реплика в диалоге Q&A (user или assistant)."""

    __tablename__ = "ask_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ask_sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped["AskSession"] = relationship(back_populates="messages")


class WebSession(Base):
    """Промежуточное состояние мастера регистрации в вебе."""

    __tablename__ = "web_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # uuid
    invite_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    tos_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    phone_code_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    qr_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    encrypted_session_draft: Mapped[str | None] = mapped_column(Text, nullable=True)
    needs_2fa: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
