"""Per-account статистика. Все запросы фильтруются по account_id."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import emoji as emoji_lib
from sqlalchemy import and_, func, select

from ..db import SessionLocal
from ..models import Chat, Message

URL_RE = re.compile(r"https?://[^\s]+")
HASHTAG_RE = re.compile(r"#[\wа-яА-ЯёЁ]+", re.UNICODE)
WORD_RE = re.compile(r"[\wа-яА-ЯёЁ]{4,}", re.UNICODE)

STOPWORDS = set(
    """
    это что как для или его все они мне про при так чтобы тоже была были если
    нас вас них нам вам ним тем чем кто где когда уже еще тут там вот вообще
    блин блять короче типа можно нужно надо очень даже кстати ладно щас сейчас
    хорошо ага угу окей привет день вечер утро ночь спасибо пожалуйста
    the and you for this that have with from your are not but was will they
    """.split()
)


def _period(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    return end - timedelta(days=days), end


# ---------------------------------------------------------------------------
#  Базовые срезы
# ---------------------------------------------------------------------------

async def total_counts(account_id: int) -> dict:
    async with SessionLocal() as s:
        msg = (await s.execute(
            select(func.count(Message.id)).where(Message.account_id == account_id)
        )).scalar_one()
        chats = (await s.execute(
            select(func.count(Chat.id)).where(Chat.account_id == account_id)
        )).scalar_one()
        tracked = (await s.execute(
            select(func.count(Chat.id)).where(
                Chat.account_id == account_id, Chat.is_tracked.is_(True)
            )
        )).scalar_one()
    return {"messages": int(msg), "chats": int(chats), "tracked": int(tracked)}


async def top_contacts(account_id: int, days: int = 1, limit: int = 15):
    start, _ = _period(days)
    async with SessionLocal() as s:
        q = (
            select(Message.sender_name, func.count(Message.id))
            .where(
                Message.account_id == account_id,
                Message.date >= start,
                Message.is_outgoing.is_(False),
                Message.sender_name.is_not(None),
            )
            .group_by(Message.sender_name)
            .order_by(func.count(Message.id).desc())
            .limit(limit)
        )
        return [(r[0], r[1]) for r in (await s.execute(q)).all()]


async def top_chats(account_id: int, days: int = 1, limit: int = 15):
    start, _ = _period(days)
    async with SessionLocal() as s:
        q = (
            select(Chat.title, func.count(Message.id))
            .join(Message, Message.chat_id == Chat.id)
            .where(Message.account_id == account_id, Message.date >= start)
            .group_by(Chat.title)
            .order_by(func.count(Message.id).desc())
            .limit(limit)
        )
        return [(r[0] or "(без названия)", r[1]) for r in (await s.execute(q)).all()]


async def hourly_activity(account_id: int, days: int = 7) -> list[int]:
    start, _ = _period(days)
    async with SessionLocal() as s:
        q = (
            select(func.extract("hour", Message.date), func.count(Message.id))
            .where(Message.account_id == account_id, Message.date >= start)
            .group_by(func.extract("hour", Message.date))
        )
        rows = (await s.execute(q)).all()
    out = [0] * 24
    for h, c in rows:
        out[int(h)] = int(c)
    return out


async def weekday_activity(account_id: int, days: int = 30) -> list[int]:
    start, _ = _period(days)
    async with SessionLocal() as s:
        q = (
            select(func.extract("dow", Message.date), func.count(Message.id))
            .where(Message.account_id == account_id, Message.date >= start)
            .group_by(func.extract("dow", Message.date))
        )
        rows = (await s.execute(q)).all()
    out = [0] * 7
    for d, c in rows:
        out[(int(d) + 6) % 7] = int(c)
    return out


async def heatmap_hour_dow(account_id: int, days: int = 30) -> list[list[int]]:
    """Возвращает 7x24 матрицу [день_недели Mon-Sun][час] = count."""
    start, _ = _period(days)
    async with SessionLocal() as s:
        q = (
            select(
                func.extract("dow", Message.date),
                func.extract("hour", Message.date),
                func.count(Message.id),
            )
            .where(Message.account_id == account_id, Message.date >= start)
            .group_by(
                func.extract("dow", Message.date),
                func.extract("hour", Message.date),
            )
        )
        rows = (await s.execute(q)).all()
    grid = [[0] * 24 for _ in range(7)]
    for d, h, c in rows:
        grid[(int(d) + 6) % 7][int(h)] = int(c)
    return grid


async def _texts(account_id: int, days: int, chat_id: int | None = None) -> list[str]:
    start, _ = _period(days)
    async with SessionLocal() as s:
        cond = [Message.account_id == account_id, Message.date >= start, Message.text != ""]
        if chat_id is not None:
            cond.append(Message.chat_id == chat_id)
        rows = (await s.execute(select(Message.text).where(and_(*cond)))).all()
    return [r[0] for r in rows]


async def top_words(account_id: int, days: int = 1, limit: int = 30):
    cnt: Counter[str] = Counter()
    for t in await _texts(account_id, days):
        for w in WORD_RE.findall(t.lower()):
            if w not in STOPWORDS:
                cnt[w] += 1
    return cnt.most_common(limit)


async def top_hashtags(account_id: int, days: int = 7, limit: int = 20):
    cnt: Counter[str] = Counter()
    for t in await _texts(account_id, days):
        for h in HASHTAG_RE.findall(t):
            cnt[h.lower()] += 1
    return cnt.most_common(limit)


async def top_links(account_id: int, days: int = 7, limit: int = 20):
    cnt: Counter[str] = Counter()
    for t in await _texts(account_id, days):
        for u in URL_RE.findall(t):
            cnt[u.split("?")[0]] += 1
    return cnt.most_common(limit)


async def top_emojis(account_id: int, days: int = 7, limit: int = 20):
    cnt: Counter[str] = Counter()
    for t in await _texts(account_id, days):
        for ch in t:
            if ch in emoji_lib.EMOJI_DATA:
                cnt[ch] += 1
    return cnt.most_common(limit)


async def media_breakdown(account_id: int, days: int = 7):
    start, _ = _period(days)
    async with SessionLocal() as s:
        q = (
            select(Message.media_type, func.count(Message.id))
            .where(
                Message.account_id == account_id,
                Message.date >= start,
                Message.has_media.is_(True),
            )
            .group_by(Message.media_type)
            .order_by(func.count(Message.id).desc())
        )
        return [(r[0] or "other", r[1]) for r in (await s.execute(q)).all()]


async def my_output(account_id: int, days: int = 7) -> dict:
    start, _ = _period(days)
    async with SessionLocal() as s:
        sent = (await s.execute(
            select(func.count(Message.id)).where(
                Message.account_id == account_id,
                Message.date >= start,
                Message.is_outgoing.is_(True),
            )
        )).scalar_one()
        received = (await s.execute(
            select(func.count(Message.id)).where(
                Message.account_id == account_id,
                Message.date >= start,
                Message.is_outgoing.is_(False),
            )
        )).scalar_one()
    return {"sent": int(sent), "received": int(received)}


async def silent_chats(account_id: int, days: int = 14, limit: int = 30):
    async with SessionLocal() as s:
        q = (
            select(Chat.title, func.max(Message.date))
            .join(Message, Message.chat_id == Chat.id, isouter=True)
            .where(Chat.account_id == account_id, Chat.is_tracked.is_(True))
            .group_by(Chat.id, Chat.title)
            .order_by(func.max(Message.date).asc().nullsfirst())
            .limit(limit)
        )
        rows = (await s.execute(q)).all()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [(r[0] or "(без названия)", r[1]) for r in rows if r[1] is None or r[1] < cutoff]


async def mentions_of_me(account_id: int, days: int = 1, limit: int = 50):
    start, _ = _period(days)
    async with SessionLocal() as s:
        q = (
            select(Chat.title, Message.sender_name, Message.text, Message.date)
            .join(Chat, Chat.id == Message.chat_id)
            .where(
                Message.account_id == account_id,
                Message.date >= start,
                Message.mentions_me.is_(True),
            )
            .order_by(Message.date.desc())
            .limit(limit)
        )
        return [(r[0] or "(чат)", r[1] or "?", r[2] or "", r[3]) for r in (await s.execute(q)).all()]


async def search(account_id: int, query: str, limit: int = 30):
    async with SessionLocal() as s:
        q = (
            select(Chat.title, Message.sender_name, Message.date, Message.text)
            .join(Chat, Chat.id == Message.chat_id)
            .where(
                Message.account_id == account_id,
                Message.text.ilike(f"%{query}%"),
            )
            .order_by(Message.date.desc())
            .limit(limit)
        )
        return [(r[0] or "?", r[1] or "?", r[2], r[3] or "") for r in (await s.execute(q)).all()]


# ---------------------------------------------------------------------------
#  ASCII-форматтеры
# ---------------------------------------------------------------------------

def bar_row(label: str, value: int, max_v: int, width: int = 12) -> str:
    if max_v <= 0:
        bar = ""
    else:
        filled = int(round(value / max_v * width))
        bar = "▇" * filled + "·" * (width - filled)
    return f"{label[:18]:<18} {value:>5}  {bar}"


def render_bars(rows: list[tuple[str, int]], width: int = 12) -> str:
    if not rows:
        return "—"
    maxv = max(c for _, c in rows)
    return "\n".join(bar_row(n, c, maxv, width) for n, c in rows)


def render_heatmap(grid: list[list[int]]) -> str:
    """7x24 → ASCII-блоки ░▒▓█."""
    flat = [c for row in grid for c in row]
    if not flat or max(flat) == 0:
        return "Нет данных"
    mx = max(flat)
    chars = " ░▒▓█"
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    header = "     " + "".join(f"{h:>2}" for h in range(0, 24, 3))
    lines = [header]
    for i, row in enumerate(grid):
        bar = ""
        for v in row:
            idx = int(round(v / mx * (len(chars) - 1)))
            bar += chars[idx] * 2
        lines.append(f"{days[i]:>3}  {bar}")
    return "\n".join(lines)


def delta(curr: int, prev: int) -> str:
    if prev == 0:
        return "🆕" if curr else ""
    pct = int(round((curr - prev) / prev * 100))
    if pct > 0:
        return f"📈 +{pct}%"
    if pct < 0:
        return f"📉 {pct}%"
    return "≈"
