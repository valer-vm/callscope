"""Общее для роутеров панели: хранилище, рабочие настройки, воркер, шаблоны, период.

Роутеры получают всё через `Depends(...)` или напрямую из `request`:

    def page(request: Request, storage: Annotated[Storage, Depends(get_storage)]): ...
        return render(request, "page.html", {...}, active="calls")

Вход проверяет middleware `auth.require_login` для всех маршрутов, кроме `/login` и
`/static/*` — роутерам ничего добавлять не нужно.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from callscope.checklist import Checklist, load_checklist
from callscope.config import AppConfig, Thresholds, load_config
from callscope.model import CallScore
from callscope.scoring import grade_for
from callscope.storage import Storage
from callscope.web.settings import WebSettings
from callscope.worker import Worker

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

NavItem = Literal["overview", "managers", "calls", "upload", "settings"]


# --- рабочие настройки из папки данных ------------------------------------------------


class WorkingSettings:
    """Рабочие `callscope.yaml` и `checklist.yaml` из папки данных.

    Перечитываются, только когда файл изменился (mtime и размер), поэтому сохранение
    в «Настройках» сразу видно и страницам, и воркеру. Ошибка в файле — ConfigError.
    """

    def __init__(self, storage: Storage) -> None:
        self._config_path = storage.config_path
        self._checklist_path = storage.checklist_path
        self._lock = threading.Lock()
        self._config: tuple[tuple[int, int], AppConfig] | None = None
        self._checklist: tuple[tuple[int, int], Checklist] | None = None

    @staticmethod
    def _stamp(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size

    def config(self) -> AppConfig:
        with self._lock:
            stamp = self._stamp(self._config_path)
            if self._config is None or self._config[0] != stamp:
                self._config = (stamp, load_config(self._config_path))
            return self._config[1]

    def checklist(self) -> Checklist:
        with self._lock:
            stamp = self._stamp(self._checklist_path)
            if self._checklist is None or self._checklist[0] != stamp:
                self._checklist = (stamp, load_checklist(self._checklist_path))
            return self._checklist[1]

    def load(self) -> tuple[AppConfig, Checklist]:
        """Для `Worker(load_settings=...)`."""
        return self.config(), self.checklist()


# --- зависимости ----------------------------------------------------------------------


def web_settings(request: Request) -> WebSettings:
    return request.app.state.settings


def get_storage(request: Request) -> Storage:
    return request.app.state.storage


def get_worker(request: Request) -> Worker | None:
    """Фоновый воркер; None — панель запущена без него (тесты)."""
    return request.app.state.worker


def get_working_settings(request: Request) -> WorkingSettings:
    return request.app.state.working_settings


def get_config(request: Request) -> AppConfig:
    return get_working_settings(request).config()


def get_checklist(request: Request) -> Checklist:
    return get_working_settings(request).checklist()


# --- выбранный период (хранится в сессии между страницами) ---------------------------

PeriodPreset = Literal["week", "4weeks", "quarter", "all", "custom"]
PERIOD_SESSION_KEY = "period"
DEFAULT_PERIOD: PeriodPreset = "4weeks"


@dataclass(frozen=True)
class PeriodChoice:
    """Выбор периода: пресет или свои даты (`custom`, границы включительно, None — без)."""

    preset: PeriodPreset = DEFAULT_PERIOD
    start: date | None = None
    end: date | None = None


def get_period_choice(request: Request) -> PeriodChoice:
    data = request.session.get(PERIOD_SESSION_KEY)
    if not isinstance(data, dict):
        return PeriodChoice()
    preset = data.get("preset")
    if preset not in ("week", "4weeks", "quarter", "all", "custom"):
        return PeriodChoice()
    if preset != "custom":
        return PeriodChoice(preset)
    try:
        start = date.fromisoformat(data["start"]) if data.get("start") else None
        end = date.fromisoformat(data["end"]) if data.get("end") else None
    except (TypeError, ValueError):
        return PeriodChoice()
    return PeriodChoice("custom", start, end)


def set_period_choice(request: Request, choice: PeriodChoice) -> None:
    request.session[PERIOD_SESSION_KEY] = {
        "preset": choice.preset,
        "start": choice.start.isoformat() if choice.start else None,
        "end": choice.end.isoformat() if choice.end else None,
    }


# --- шаблоны -------------------------------------------------------------------------


def format_datetime(value: datetime | None) -> str:
    """«16.10.2026 14:30»; время с часовым поясом показывается в местном."""
    if value is None:
        return "—"
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.strftime("%d.%m.%Y %H:%M")


def format_date(value: date | datetime | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime) and value.tzinfo is not None:
        value = value.astimezone()
    return value.strftime("%d.%m.%Y")


def format_score(value: float | CallScore | None) -> str:
    """Балл без дробной части, как в отчёте этапа 1; нет балла — «—»."""
    if isinstance(value, CallScore):
        value = value.score
    if value is None:
        return "—"
    return f"{value:.0f}"


def score_grade(value: float | CallScore | None, thresholds: Thresholds | None = None) -> str:
    """CSS-класс балла: good / medium / bad (как `.score.good` в отчёте); нет балла — na."""
    if isinstance(value, CallScore):
        return value.grade
    if value is None:
        return "na"
    return grade_for(value, thresholds or Thresholds())


def format_percent(value: float | None) -> str:
    """Доля 0..1 → «37%»."""
    if value is None:
        return "—"
    return f"{value * 100:.0f}%"


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = templates.env
    env.filters["dt"] = format_datetime
    env.filters["date_ru"] = format_date
    env.filters["score"] = format_score
    env.filters["grade"] = score_grade
    env.filters["percent"] = format_percent
    return templates


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    active: NavItem | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Страница в каркасе `base.html`. В шаблоне всегда есть `demo`, `active`, `request`."""
    settings = web_settings(request)
    full = {"demo": settings.demo, "active": active, **(context or {})}
    return get_templates(request).TemplateResponse(
        request, name, full, status_code=status_code
    )

