"""FastAPI приложение для регистрации (бета-доступ)."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..config import get_settings
from .routes import api, legal, registration

settings = get_settings()

THIS = Path(__file__).resolve().parent
TEMPLATES_DIR = THIS / "templates"
STATIC_DIR = THIS / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app() -> FastAPI:
    app = FastAPI(title="TG Summarizer · Beta", docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.web_secret_key,
        same_site="lax",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.state.templates = templates

    app.include_router(registration.router)
    app.include_router(legal.router)
    app.include_router(api.router)

    @app.get("/", response_class=HTMLResponse)
    async def landing(request: Request):
        return templates.TemplateResponse(
            "landing.html",
            {"request": request, "bot": settings.control_bot_username},
        )

    async def _serve_mini_app(request: Request):
        resp = templates.TemplateResponse(
            "mini_app.html",
            {"request": request, "bot": settings.control_bot_username},
        )
        # Запрещаем кеширование Mini App, чтобы обновления подхватывались сразу
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.get("/app", response_class=HTMLResponse)
    async def mini_app(request: Request):
        return await _serve_mini_app(request)

    # Версионированный путь — нужен чтобы Telegram считал каждый деплой
    # отдельным URL и не отдавал кешированный HTML.
    @app.get("/app/v{version}", response_class=HTMLResponse)
    async def mini_app_versioned(version: str, request: Request):
        return await _serve_mini_app(request)

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    return app
