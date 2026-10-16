"""«Обзор» — цифры за период, воронка по пунктам, динамика по неделям; выбор периода."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from callscope import stats
from callscope.checklist import Checklist
from callscope.config import AppConfig
from callscope.storage import Storage
from callscope.web import charts
from callscope.web.deps import (
    PeriodChoice,
    get_checklist,
    get_config,
    get_period_choice,
    get_storage,
    render,
    set_period_choice,
)

router = APIRouter()

PRESETS = ("week", "4weeks", "quarter", "all")


def current_period(request: Request) -> tuple[PeriodChoice, stats.Period]:
    """Выбор периода из сессии и сам период (для фильтров и подписей)."""
    choice = get_period_choice(request)
    if choice.preset == "custom":
        return choice, stats.period_custom(choice.start, choice.end)
    return choice, stats.period_preset(choice.preset, date.today())


def safe_next(value: str | None) -> str:
    """Возврат только на свой путь: «/…», не «//…» и не внешний адрес."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@router.post("/period")
def choose_period(
    request: Request,
    preset: Annotated[str, Form()] = "4weeks",
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",  # noqa: A002 — имя поля формы
) -> RedirectResponse:
    if preset == "custom":
        first, last = _parse_date(start), _parse_date(end)
        if first and last and first > last:
            first, last = last, first
        if first is None and last is None:
            set_period_choice(request, PeriodChoice("all"))
        else:
            set_period_choice(request, PeriodChoice("custom", first, last))
    elif preset in PRESETS:
        set_period_choice(request, PeriodChoice(preset))  # type: ignore[arg-type]
    return RedirectResponse(safe_next(next), status_code=303)


@router.get("/", response_class=HTMLResponse)
def overview_page(
    request: Request,
    storage: Annotated[Storage, Depends(get_storage)],
    config: Annotated[AppConfig, Depends(get_config)],
    checklist: Annotated[Checklist, Depends(get_checklist)],
) -> HTMLResponse:
    choice, period = current_period(request)
    calls = storage.list_calls(stats.CallFilters(period))
    data = stats.overview(calls, checklist, config.thresholds, period)
    top_lost = max(data.funnel, key=lambda s: s.lost, default=None)
    if top_lost is not None and top_lost.lost == 0:
        top_lost = None
    context = {
        "page_title": "Обзор",
        "data": data,
        "period": period,
        "period_choice": choice,
        "next_url": "/",
        "thresholds": config.thresholds,
        "top_lost": top_lost,
        "line_svg": charts.line_chart(data.weekly, config.thresholds.good),
        "funnel_svg": charts.funnel_chart(
            data.funnel, data.calls, data.top_failure.item_id if data.top_failure else None
        ),
    }
    return render(request, "overview.html", context, active="overview")
