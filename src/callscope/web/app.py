"""Сборка приложения панели: хранилище, сессии, правило входа, роутеры, статика."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
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

ERROR_TEXTS = {
    400: "Запрос не удалось выполнить: неверные данные.",
    404: "Такой страницы нет.",
    405: "Это действие здесь недоступно.",
    409: "Действие сейчас невозможно.",
    422: "Неверный адрес или данные запроса.",
}


def _wants_page(request: Request) -> bool:
    """Ошибку показываем страницей, если это не API, не аудио, не Excel и не статика."""
    path = request.url.path
    return not auth.is_api_path(path) and not path.startswith(auth.PUBLIC_PREFIXES)


def _error_text(status_code: int, detail: object) -> str:
    if isinstance(detail, str) and detail.strip():
        try:
            standard = HTTPStatus(status_code).phrase
        except ValueError:
            standard = ""
        if detail != standard:  # своё русское сообщение маршрута, а не «Not Found»
            return detail
    return ERROR_TEXTS.get(status_code, "Не удалось выполнить запрос.")


def _error_page(request: Request, status_code: int, message: str) -> Response:
    return render(
        request,
        "base.html",
        {"page_title": "Ошибка", "error_message": message},
        status_code=status_code,
    )


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
    app.middleware("http")(auth.check_same_origin)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=SESSION_COOKIE,
        max_age=settings.session_days * 24 * 3600,
        same_site="lax",
        https_only=settings.cookie_secure,
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
        return _error_page(request, 500, exc.message_ru)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if not _wants_page(request):
            return await http_exception_handler(request, exc)
        response = _error_page(request, exc.status_code, _error_text(exc.status_code, exc.detail))
        for key, value in (exc.headers or {}).items():
            response.headers[key] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        if not _wants_page(request):
            return await request_validation_exception_handler(request, exc)
        return _error_page(request, 422, ERROR_TEXTS[422])

    return app
