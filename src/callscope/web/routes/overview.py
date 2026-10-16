"""«Обзор» — цифры за период, воронка по пунктам, динамика по неделям (таск 03)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from callscope.web.deps import render_stub

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def overview_page(request: Request) -> HTMLResponse:
    return render_stub(request, "Обзор", "overview")
