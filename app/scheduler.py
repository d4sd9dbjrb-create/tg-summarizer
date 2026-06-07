"""Per-account scheduler: ежедневные/еженедельные дайджесты."""
from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .bot import notify, send_long, _open_app_kb
from .config import get_settings
from .db import SessionLocal
from .features import summary
from .models import Account

log = logging.getLogger(__name__)
settings = get_settings()


async def _job_every_hour():
    """Раз в час: для каждого активного аккаунта проверяем не пора ли слать daily."""
    now_h = datetime.now().astimezone().hour  # local hour from process tz; для прод используется TIMEZONE через APScheduler
    async with SessionLocal() as s:
        accs = (
            await s.execute(
                select(Account).where(
                    Account.status == "active",
                    Account.bot_linked_at.is_not(None),
                    Account.daily_digest_hour == now_h,
                )
            )
        ).scalars().all()
    for acc in accs:
        try:
            await notify(acc.tg_user_id, "📅 <b>Дайджест за день</b>")
            text = await summary.daily_summary(acc.id)
            await send_long(acc.tg_user_id, text, reply_markup=_open_app_kb())
        except Exception:  # noqa: BLE001
            log.exception("daily digest failed for account %s", acc.id)


async def _job_weekly():
    async with SessionLocal() as s:
        accs = (
            await s.execute(
                select(Account).where(
                    Account.status == "active",
                    Account.bot_linked_at.is_not(None),
                )
            )
        ).scalars().all()
    for acc in accs:
        try:
            await notify(acc.tg_user_id, "🗓 <b>Дайджест за неделю</b>")
            text = await summary.weekly_summary(acc.id)
            await send_long(acc.tg_user_id, text)
            text2 = await summary.important_week(acc.id)
            await notify(acc.tg_user_id, "🔥 <b>Важное за неделю</b>")
            await send_long(acc.tg_user_id, text2, reply_markup=_open_app_kb())
        except Exception:  # noqa: BLE001
            log.exception("weekly digest failed for account %s", acc.id)


def build_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=settings.timezone)
    # каждый час в :00 проверяем, у кого сейчас daily-час
    sched.add_job(_job_every_hour, CronTrigger(minute=0), id="hourly-digest", replace_existing=True)
    # понедельник 09:05
    sched.add_job(_job_weekly, CronTrigger(day_of_week="mon", hour=9, minute=5), id="weekly", replace_existing=True)
    return sched
