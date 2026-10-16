"""Вход по общему паролю: сессия в подписанной cookie, блокировка перебора, правило доступа.

Без сессии HTML-страница отвечает 303 на `/login?next=…`, а `/api/*`, `/audio/*` и
выгрузки (`*.xlsx`) — 401. Публичны только `/login` и `/static/*`.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from callscope.web.deps import render, web_settings

SESSION_AUTH_KEY = "auth"
SESSION_ID_KEY = "sid"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PUBLIC_PATHS = frozenset({"/login"})
PUBLIC_PREFIXES = ("/static/",)
API_PREFIXES = ("/api/", "/audio/")
API_SUFFIXES = (".xlsx",)


def password_matches(given: str, expected: str) -> bool:
    """Сравнение постоянного времени (не выдаёт по задержке, сколько символов совпало)."""
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def password_fingerprint(password: str, secret_key: str) -> str:
    """Отпечаток пароля в сессии: сменили пароль в `.env` — старые сессии недействительны."""
    return hmac.new(secret_key.encode(), password.encode(), hashlib.sha256).hexdigest()[:32]


def is_authenticated(request: Request) -> bool:
    settings = web_settings(request)
    stored = request.session.get(SESSION_AUTH_KEY)
    expected = password_fingerprint(settings.password, settings.secret_key)
    if not (isinstance(stored, str) and hmac.compare_digest(stored, expected)):
        return False
    sid = request.session.get(SESSION_ID_KEY)
    if not isinstance(sid, str) or not sid:
        return False
    # «Выйти» отзывает сессию на сервере: скопированная cookie тоже перестаёт работать.
    return not request.app.state.storage.is_session_revoked(sid)


def is_api_path(path: str) -> bool:
    return path.startswith(API_PREFIXES) or path.endswith(API_SUFFIXES)


def safe_next(target: str | None) -> str:
    """Только путь внутри панели: «/…», не «//…» и не «/\\…» (иначе — открытый редирект)."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    if any(ch in target for ch in "\r\n"):
        return "/"
    return target


async def require_login(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """HTTP-middleware: пропускает публичное, остальное — только с действующей сессией."""
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES) or is_authenticated(request):
        return await call_next(request)
    if is_api_path(path):
        return JSONResponse({"detail": "Нужен вход в панель."}, status_code=401)
    if request.method in ("GET", "HEAD"):
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
    return RedirectResponse("/login", status_code=303)


async def check_same_origin(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Защита от подделки запросов: изменяющий запрос (POST и т. п.) из браузера должен
    прийти со страницы самой панели. Браузер ставит Origin (или Referer); если они указывают
    на другой адрес — 403. Запросы без обоих заголовков (не из браузера) пропускаются."""
    if request.method in UNSAFE_METHODS:
        source = request.headers.get("origin") or request.headers.get("referer")
        if source is not None and not _same_host(source, request):
            message = "Запрос пришёл не со страницы панели и отклонён."
            if is_api_path(request.url.path):
                return JSONResponse({"detail": message}, status_code=403)
            return render(
                request, "base.html", {"page_title": "Ошибка", "error_message": message},
                status_code=403,
            )
    return await call_next(request)


def _same_host(source: str, request: Request) -> bool:
    """Сравнивается адрес с портом (схема не важна: за HTTPS-прокси сервер видит http)."""
    if source == "null":
        return False
    try:
        netloc = urlsplit(source).netloc.lower()
    except ValueError:
        return False
    host = request.headers.get("host", "").lower()
    return bool(netloc) and netloc == host


class LoginLimiter:
    """Неверные пароли подряд с одного адреса; после `attempts` — блок на `minutes`."""

    def __init__(self, attempts: int, minutes: int, clock: Callable[[], float] = time.monotonic):
        self.attempts = attempts
        self.seconds = minutes * 60
        self.clock = clock
        self._failures: dict[str, tuple[int, float]] = {}  # адрес → (подряд, когда последняя)
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def _forget_stale(self, now: float) -> None:
        """Старые записи не копятся: неудачи старше окна блокировки и истёкшие блоки."""
        for address in [a for a, (_, at) in self._failures.items() if now - at > self.seconds]:
            del self._failures[address]
        for address in [a for a, until in self._blocked_until.items() if until <= now]:
            del self._blocked_until[address]

    def blocked_for(self, address: str) -> float:
        """Сколько секунд ещё заблокирован адрес (0 — не заблокирован)."""
        with self._lock:
            until = self._blocked_until.get(address)
            if until is None:
                return 0.0
            left = until - self.clock()
            if left <= 0:
                del self._blocked_until[address]
                return 0.0
            return left

    def failure(self, address: str) -> float:
        """Учесть неверный пароль; вернуть длительность блокировки, если она началась."""
        with self._lock:
            now = self.clock()
            self._forget_stale(now)
            count = self._failures.get(address, (0, now))[0] + 1
            if count >= self.attempts:
                self._failures.pop(address, None)
                self._blocked_until[address] = now + self.seconds
                return float(self.seconds)
            self._failures[address] = (count, now)
            return 0.0

    def tracked(self) -> int:
        """Сколько адресов сейчас помнится (для проверки, что память не растёт)."""
        with self._lock:
            return len(self._failures) + len(self._blocked_until)

    def success(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)


def _client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _blocked_message(seconds_left: float) -> str:
    minutes = max(1, math.ceil(seconds_left / 60))
    until = (datetime.now() + timedelta(seconds=seconds_left)).strftime("%H:%M")
    return (
        f"Слишком много неверных попыток. Вход заблокирован на {minutes} мин — "
        f"попробуйте снова после {until}."
    )


router = APIRouter()


@router.get("/login")
def login_page(request: Request, next: str | None = None) -> Response:
    if is_authenticated(request):
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", {"next": safe_next(next), "error": None})


@router.post("/login")
def login_submit(
    request: Request,
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    limiter: LoginLimiter = request.app.state.login_limiter
    address = _client_address(request)
    target = safe_next(next)

    left = limiter.blocked_for(address)
    if left:
        return _login_error(request, target, _blocked_message(left), 429)

    settings = web_settings(request)
    if not password_matches(password, settings.password):
        blocked = limiter.failure(address)
        if blocked:
            return _login_error(request, target, _blocked_message(blocked), 429)
        return _login_error(request, target, "Неверный пароль. Попробуйте ещё раз.", 401)

    limiter.success(address)
    request.session.clear()
    request.session[SESSION_AUTH_KEY] = password_fingerprint(
        settings.password, settings.secret_key
    )
    request.session[SESSION_ID_KEY] = secrets.token_urlsafe(24)
    return RedirectResponse(target, status_code=303)


@router.post("/logout")
def logout(request: Request) -> Response:
    sid = request.session.get(SESSION_ID_KEY)
    if isinstance(sid, str) and sid:
        keep = web_settings(request).session_days + 1
        request.app.state.storage.revoke_session(sid, keep_days=keep)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _login_error(request: Request, target: str, message: str, status: int) -> Response:
    return render(request, "login.html", {"next": target, "error": message}, status_code=status)
