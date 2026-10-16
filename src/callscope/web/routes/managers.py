"""«Менеджеры» — рейтинг и страница менеджера (таск 03)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from callscope.web.deps import render_stub

router = APIRouter()


@router.get("/managers", response_class=HTMLResponse)
def managers_page(request: Request) -> HTMLResponse:
    return render_stub(request, "Менеджеры", "managers")


@router.get("/managers/{name}", response_class=HTMLResponse)
def manager_page(request: Request, name: str) -> HTMLResponse:
    return render_stub(request, "Менеджер", "managers")
