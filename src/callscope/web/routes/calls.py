"""«Звонки» — список с фильтрами, страница звонка, правки, выгрузка (таск 04)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from callscope.web.deps import render_stub

router = APIRouter()


@router.get("/calls", response_class=HTMLResponse)
def calls_page(request: Request) -> HTMLResponse:
    return render_stub(request, "Звонки", "calls")


@router.get("/calls/{call_id}", response_class=HTMLResponse)
def call_page(request: Request, call_id: int) -> HTMLResponse:
    return render_stub(request, "Звонок", "calls")
