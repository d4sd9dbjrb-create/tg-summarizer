"""Точка входа: web регистрации + control bot + менеджер userbot'ов + scheduler."""
from __future__ import annotations

import asyncio
import logging

import uvicorn

from . import account_manager
from .bot import start_bot
from .config import get_settings
from .db import init_db
from .scheduler import build_scheduler
from .web.app import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")
settings = get_settings()


async def _run_web():
    config = uvicorn.Config(
        create_app(),
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    log.info("Init DB...")
    await init_db()

    log.info("Starting all userbot clients...")
    await account_manager.start_all()

    log.info("Starting scheduler...")
    sched = build_scheduler()
    sched.start()

    log.info("Launching web + control bot")
    tasks = [
        asyncio.create_task(_run_web(), name="web"),
        asyncio.create_task(start_bot(), name="control_bot"),
    ]
    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down...")
    finally:
        sched.shutdown(wait=False)
        await account_manager.stop_all()


if __name__ == "__main__":
    asyncio.run(main())
