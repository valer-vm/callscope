"""«Настройки» — чек-лист, переоценка, шаблон имени файла (таск 05)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from callscope.web.deps import render_stub

router = APIRouter()


@router.get("/settings")
def settings_root() -> RedirectResponse:
    return RedirectResponse("/settings/checklist", status_code=303)


@router.get("/settings/checklist", response_class=HTMLResponse)
def checklist_page(request: Request) -> HTMLResponse:
    return render_stub(request, "Настройки — чек-лист", "settings")


@router.get("/settings/filename", response_class=HTMLResponse)
def filename_page(request: Request) -> HTMLResponse:
    return render_stub(request, "Настройки — имя файла", "settings")
