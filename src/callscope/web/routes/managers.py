"""«Менеджеры» — рейтинг за период и страница менеджера."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from callscope import stats
from callscope.checklist import Checklist
from callscope.config import AppConfig
from callscope.storage import Storage
from callscope.web import charts
from callscope.web.deps import get_checklist, get_config, get_storage, render
from callscope.web.routes.overview import current_period

router = APIRouter()


@router.get("/managers", response_class=HTMLResponse)
def managers_page(
    request: Request,
    storage: Annotated[Storage, Depends(get_storage)],
    config: Annotated[AppConfig, Depends(get_config)],
    checklist: Annotated[Checklist, Depends(get_checklist)],
) -> HTMLResponse:
    choice, period = current_period(request)
    calls = storage.list_calls(stats.CallFilters(period))
    context = {
        "page_title": "Менеджеры",
        "rows": stats.manager_ranking(calls, checklist),
        "titles": {i.id: i.title for i in checklist.items},
        "period": period,
        "period_choice": choice,
        "next_url": "/managers",
        "thresholds": config.thresholds,
    }
    return render(request, "managers.html", context, active="managers")


@router.get("/managers/{name}", response_class=HTMLResponse)
def manager_page(
    request: Request,
    name: str,
    storage: Annotated[Storage, Depends(get_storage)],
    config: Annotated[AppConfig, Depends(get_config)],
    checklist: Annotated[Checklist, Depends(get_checklist)],
) -> HTMLResponse:
    choice, period = current_period(request)
    ever = storage.list_calls(stats.CallFilters(stats.Period(None, None, ""), manager=name))
    if not ever:
        return render(
            request,
            "base.html",
            {"page_title": "Менеджер", "error_message": f"Менеджер «{name}» не найден."},
            active="managers",
            status_code=404,
        )
    calls = storage.list_calls(stats.CallFilters(period, manager=name))
    titles = {i.id: i.title for i in checklist.items}
    rows = stats.manager_ranking(calls, checklist)
    row = rows[0] if rows else None
    points = stats.weekly(calls, period)
    context = {
        "page_title": name,
        "name": name,
        "calls": calls,
        "row": row,
        "failures": [(titles.get(item, item), count) for item, count in row.top_failures]
        if row
        else [],
        "titles": titles,
        "period": period,
        "period_choice": choice,
        "next_url": f"/managers/{quote(name, safe='')}",
        "thresholds": config.thresholds,
        "line_svg": charts.line_chart(points, config.thresholds.good),
    }
    return render(request, "manager.html", context, active="managers")
