# 📨 TG Summarizer (multi-tenant beta)

Сервис, который подключается к Telegram-аккаунту пользователя как **userbot**, читает чаты и каналы, и через управляющий бот выдаёт:

- ежедневные / еженедельные сводки + on-demand catch-up;
- богатую статистику (люди, чаты, время, слова, медиа, sentiment);
- топ-важное за неделю, action-items, упоминания, keyword-алерты;
- профили контактов с LLM-анализом стиля общения *(этап B)*;
- reading list ссылок, sentiment-тренды, auto-tag чатов *(этап B)*.

## Архитектура

- **FastAPI веб-фронт** — только регистрация: invite-код → ToS → QR/Phone → код / 2FA → deep-link в бот.
- **Telethon мульти-клиенты** — на каждый зарегистрированный аккаунт свой `TelegramClient` со `StringSession`, зашифрованным Fernet'ом и хранимым в Postgres.
- **aiogram 3 control bot** — единый бот, gating пускает только связанных юзеров. FSM-меню, inline-кнопки, period-селектор для статистики.
- **APScheduler** — раз в час проверяет, у кого подошёл `daily_digest_hour`; по понедельникам — weekly + важное.
- **PostgreSQL** через SQLAlchemy 2 (async) + asyncpg.
- **LLM** — DeepSeek или Gemini, ключи в .env (на бете один ключ на всех).

## Запуск (локально)

```bash
cp .env.example .env

# 1. сгенерируй ключ шифрования сессий:
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
# вставь в .env как SESSION_ENCRYPTION_KEY

# 2. сгенерируй WEB_SECRET_KEY:
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'

# 3. заполни TG_API_ID / TG_API_HASH (https://my.telegram.org)
# 4. создай бота в @BotFather, заполни CONTROL_BOT_TOKEN и CONTROL_BOT_USERNAME
# 5. узнай свой Telegram user_id (@userinfobot) → ADMIN_USER_ID
# 6. придумай INVITE_CODE
# 7. возьми ключ DeepSeek (https://platform.deepseek.com) или Gemini (https://aistudio.google.com)

docker compose up --build
```

Открой <http://localhost:8000> → введи invite-код → прими условия → войди в Telegram (QR или телефон) → получи deep-link → открой бот → начни пользоваться.

## Безопасность

- Telethon `StringSession` шифруется AES (Fernet) перед записью в БД.
- В `bot.py` все запросы фильтруются по `account_id` владельца — изоляция данных.
- Бот отвечает только тем `tg_user_id`, у которых есть привязанный `Account` со статусом `active`.
- Команды `/logout` и `/delete_me CONFIRM` отзывают сессию и удаляют все данные.

## 30+ функций

Реализовано на этапе A:

1. Daily / Weekly / On-demand сводки
2. Catch-up за N часов
3. Topics-кластеризация
4. Action-items
5. **Важное за неделю** (топ-10 событий + что сделать)
6. Mentions / keyword-алерты в реальном времени
7. Топ контактов / чатов / слов / хэштегов / ссылок / эмодзи
8. Hourly + weekday + heatmap (день × час) ASCII-визуализация
9. Media breakdown / my output / тихие чаты
10. Полнотекстовый поиск по архиву
11. Track / mute / whitelist чатов
12. Перевод сводок
13. ASCII-бары и дельты vs прошлый период
14. Per-account настройки (час дайджеста, провайдер LLM, auto_tag/sentiment вкл/выкл)
15. /logout, /delete_me, /export

Запланировано на этапе B (помечено в коде / меню):
- LLM enrichment-воркер: sentiment, auto_tag, importance_score, is_action_item — батчами раз в час
- Профили контактов с narrative / talking_points / avoid_topics
- Reading list с TL;DR форвардов и парсингом title/og:description
- Календарь активности за год
- Рекорды (streaks, самый длинный диалог, …)
- Auto-tag чатов и фильтрация сводок по тегу
- Экспорт в JSON

## Команды бота

```
/menu       главное меню
/daily      сводка за сутки
/weekly     за неделю
/catchup N  что я пропустил за N часов
/important  топ-10 важного за неделю
/actions    to-do
/mentions   упоминания
/search     поиск по архиву
/listchats  список чатов с tg_chat_id
/track / /untrack / /mute / /unmute <tg_chat_id>
/kw_add /kw_list /kw_del   keyword-алерты
/translate  reply на сводку → перевод
/digest_hour 0..23
/llm deepseek|gemini
/logout     отзыв сессии
/delete_me CONFIRM  полное удаление
```

## Лицензия

MIT. Используй на свой риск. Userbot формально нарушает ToS Telegram.
