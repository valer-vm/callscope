"""Сборка приложения панели: хранилище, сессии, правило входа, роутеры, статика."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from callscope.errors import CallScopeError
from callscope.storage import Storage
from callscope.web import auth
from callscope.web.deps import STATIC_DIR, WorkingSettings, make_templates, render
from callscope.web.routes import calls, managers, overview, upload
from callscope.web.routes import settings as settings_routes
from callscope.web.settings import WebSettings
from callscope.worker import Worker

log = logging.getLogger("callscope")

SESSION_COOKIE = "callscope_session"


def create_app(settings: WebSettings, worker: Worker | None = None) -> FastAPI:
    """Приложение панели. Воркер (если передан) запускается и останавливается вместе с ним."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if worker is not None:
            worker.start()
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()

    app = FastAPI(
        title="CallScope",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    storage = Storage(settings.data_dir)
    app.state.settings = settings
    app.state.storage = storage
    app.state.worker = worker
    app.state.working_settings = WorkingSettings(storage)
    app.state.templates = make_templates()
    app.state.login_limiter = auth.LoginLimiter(
        settings.lockout_attempts, settings.lockout_minutes
    )

    # Порядок важен: последним добавлен — первым выполняется. Сессия нужна правилу входа.
    app.middleware("http")(auth.require_login)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=SESSION_COOKIE,
        max_age=settings.session_days * 24 * 3600,
        same_site="lax",
        https_only=False,
    )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(auth.router)
    app.include_router(upload.router)
    app.include_router(overview.router)
    app.include_router(managers.router)
    app.include_router(calls.router)
    app.include_router(settings_routes.router)

    @app.exception_handler(CallScopeError)
    async def callscope_error(request: Request, exc: CallScopeError) -> Response:
        log.error("%s: %s", request.url.path, exc.message_ru)
        if auth.is_api_path(request.url.path):
            return JSONResponse({"detail": exc.message_ru}, status_code=500)
        return render(
            request,
            "base.html",
            {"page_title": "Ошибка", "error_message": exc.message_ru},
            status_code=500,
        )

    return app
