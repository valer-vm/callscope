"""Настройки веб-панели из `.env` и окружения: пароль, ключ подписи сессий, папка данных."""

from __future__ import annotations

import contextlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from callscope.errors import ConfigError

ENV_PASSWORD = "CALLSCOPE_PASSWORD"
ENV_SECRET_KEY = "CALLSCOPE_SECRET_KEY"
ENV_DATA_DIR = "CALLSCOPE_DATA_DIR"
ENV_HOST = "CALLSCOPE_HOST"
ENV_PORT = "CALLSCOPE_PORT"
ENV_TRUSTED_PROXIES = "CALLSCOPE_TRUSTED_PROXIES"
ENV_COOKIE_SECURE = "CALLSCOPE_COOKIE_SECURE"

DEFAULT_DATA_DIR = Path("callscope-data")
DEFAULT_DEMO_DATA_DIR = Path("callscope-data-demo")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
SECRET_KEY_FILE = ".secret_key"


@dataclass(frozen=True)
class WebSettings:
    data_dir: Path
    password: str
    secret_key: str
    demo: bool = False
    session_days: int = 30
    lockout_attempts: int = 5
    lockout_minutes: int = 5
    cookie_secure: bool = False  # cookie только по HTTPS (панель за HTTPS-прокси)

    def __repr__(self) -> str:  # секреты не попадают в журналы и трассировки
        return f"WebSettings(data_dir={self.data_dir!s}, demo={self.demo})"


def load_env() -> None:
    """`.env` из текущей папки; уже заданные переменные окружения важнее."""
    load_dotenv(Path.cwd() / ".env", override=False)


def resolve_data_dir(data_dir: Path | None, demo: bool) -> Path:
    """--data-dir → CALLSCOPE_DATA_DIR → ./callscope-data (в демо — ./callscope-data-demo)."""
    if data_dir is not None:
        return Path(data_dir)
    if demo:
        return DEFAULT_DEMO_DATA_DIR
    env = os.environ.get(ENV_DATA_DIR, "").strip()
    return Path(env) if env else DEFAULT_DATA_DIR


def load_web_settings(data_dir: Path | None, demo: bool) -> WebSettings:
    """Читает `.env`/окружение. Нет пароля → ConfigError с подсказкой, что вписать в `.env`.
    Пустой CALLSCOPE_SECRET_KEY — ключ генерируется один раз и хранится в папке данных."""
    load_env()
    password = os.environ.get(ENV_PASSWORD, "")
    if not password.strip():
        raise ConfigError(
            f"Не задан пароль панели. Скопируйте .env.example в .env и впишите пароль "
            f"в строку {ENV_PASSWORD}=, затем запустите снова."
        )
    folder = resolve_data_dir(data_dir, demo)
    secret = os.environ.get(ENV_SECRET_KEY, "").strip() or _stored_secret(folder)
    return WebSettings(
        data_dir=folder,
        password=password,
        secret_key=secret,
        demo=demo,
        cookie_secure=env_flag(ENV_COOKIE_SECURE),
    )


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "да")


def trusted_proxies() -> str | None:
    """Адреса обратного прокси, которому можно верить в X-Forwarded-For (через запятую).
    Пусто — заголовки прокси не учитываются (иначе их подделка обходит блокировку входа)."""
    value = os.environ.get(ENV_TRUSTED_PROXIES, "").strip()
    return value or None


def _stored_secret(folder: Path) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SECRET_KEY_FILE
    with contextlib.suppress(OSError):
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    secret = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(secret)
    return secret


def server_address(host: str | None, port: int | None) -> tuple[str, int]:
    """Флаг → CALLSCOPE_HOST/CALLSCOPE_PORT → 127.0.0.1:8000."""
    resolved_host = host or os.environ.get(ENV_HOST, "").strip() or DEFAULT_HOST
    if port is not None:
        return resolved_host, port
    env_port = os.environ.get(ENV_PORT, "").strip()
    if env_port:
        try:
            return resolved_host, int(env_port)
        except ValueError:
            raise ConfigError(f"{ENV_PORT} должен быть числом, сейчас «{env_port}».") from None
    return resolved_host, DEFAULT_PORT
