"""«Звонки» — список с фильтрами, страница звонка, правки, смена сторон, удаление, Excel."""

from __future__ import annotations

import tempfile
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from callscope import rescoring, stats
from callscope.aggregate import build_summary
from callscope.checklist import Checklist
from callscope.config import AppConfig
from callscope.errors import CallScopeError
from callscope.report_xlsx import write_xlsx
from callscope.storage import CallRecord, Storage
from callscope.textnorm import Normalizer
from callscope.web.deps import get_checklist, get_config, get_storage, render
from callscope.web.routes.overview import current_period

router = APIRouter()

PAGE_SIZE = 50
PERIOD_PRESETS = ("week", "4weeks", "quarter", "all")
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_normalizer_lock = threading.Lock()
_normalizer: Normalizer | None = None


def _get_normalizer() -> Normalizer:
    """Один Normalizer на процесс страниц (pymorphy грузится секунду-другую)."""
    global _normalizer
    with _normalizer_lock:
        if _normalizer is None:
            _normalizer = Normalizer()
        return _normalizer


# --- фильтры из адреса ------------------------------------------------------------------


@dataclass(frozen=True)
class FilterForm:
    """Значения формы фильтров (как в адресе) и собранные из них `CallFilters`."""

    period: str  # week | 4weeks | quarter | all | custom
    start: date | None
    end: date | None
    manager: str
    score_min: float | None
    score_max: float | None
    failed_item: str
    filters: stats.CallFilters


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _number(value: str | None) -> float | None:
    try:
        return float(value.replace(",", ".")) if value and value.strip() else None
    except ValueError:
        return None


def parse_filters(request: Request) -> FilterForm:
    """Фильтры из адреса; нет `period` — берётся период из сессии (как на «Обзоре»)."""
    query = request.query_params
    preset = query.get("period", "")
    start, end = _date(query.get("start")), _date(query.get("end"))
    if preset == "custom":
        period = stats.period_custom(start, end)
    elif preset in PERIOD_PRESETS:
        period = stats.period_preset(preset, date.today())  # type: ignore[arg-type]
    else:
        choice, period = current_period(request)
        preset, start, end = choice.preset, choice.start, choice.end
    manager = (query.get("manager") or "").strip()
    failed_item = (query.get("failed_item") or "").strip()
    score_min, score_max = _number(query.get("score_min")), _number(query.get("score_max"))
    filters = stats.CallFilters(
        period=period,
        manager=manager or None,
        score_min=score_min,
        score_max=score_max,
        failed_item=failed_item or None,
    )
    return FilterForm(preset, start, end, manager, score_min, score_max, failed_item, filters)


def _query_string(form: FilterForm, **extra: object) -> str:
    """Адресная строка с теми же фильтрами (для страниц и ссылки на Excel)."""
    pairs: dict[str, object] = {"period": form.period}
    if form.period == "custom":
        pairs["start"] = form.start.isoformat() if form.start else ""
        pairs["end"] = form.end.isoformat() if form.end else ""
    pairs["manager"] = form.manager
    pairs["score_min"] = "" if form.score_min is None else f"{form.score_min:g}"
    pairs["score_max"] = "" if form.score_max is None else f"{form.score_max:g}"
    pairs["failed_item"] = form.failed_item
    pairs.update(extra)
    return urlencode({k: v for k, v in pairs.items() if v not in ("", None)})


# --- список -----------------------------------------------------------------------------


@router.get("/calls", response_class=HTMLResponse)
def calls_page(
    request: Request,
    storage: Annotated[Storage, Depends(get_storage)],
    config: Annotated[AppConfig, Depends(get_config)],
    checklist: Annotated[Checklist, Depends(get_checklist)],
) -> HTMLResponse:
    form = parse_filters(request)
    found = storage.list_calls(form.filters)
    pages = max(1, -(-len(found) // PAGE_SIZE))
    try:
        page = int(request.query_params.get("page", "1"))
    except ValueError:
        page = 1
    page = min(max(page, 1), pages)
    chunk = found[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    # «Стороны приблизительно» лежит в диалоге, а в списке его нет — читаем только для страницы.
    approximate: set[int] = set()
    for record in chunk:
        full = storage.get(record.id)
        if full is not None and full.dialogue is not None and full.dialogue.approximate:
            approximate.add(record.id)

    everything = storage.list_calls()
    managers = sorted({c.manager for c in everything if c.status == "done"})
    pending = sum(1 for c in everything if c.status != "done")
    context = {
        "page_title": "Звонки",
        "form": form,
        "rows": chunk,
        "total": len(found),
        "page": page,
        "pages": pages,
        "approximate": approximate,
        "managers": managers,
        "items": checklist.items,
        "titles": {i.id: i.title for i in checklist.items},
        "thresholds": config.thresholds,
        "pending": pending,
        "period": form.filters.period,
        "export_url": "/calls/export.xlsx?" + _query_string(form),
        "prev_url": "/calls?" + _query_string(form, page=page - 1) if page > 1 else None,
        "next_url": "/calls?" + _query_string(form, page=page + 1) if page < pages else None,
        "has_filters": bool(form.manager or form.failed_item)
        or form.score_min is not None
        or form.score_max is not None,
    }
    return render(request, "calls.html", context, active="calls")


# --- Excel ------------------------------------------------------------------------------


@router.get("/calls/export.xlsx")
def export_xlsx(
    request: Request,
    storage: Annotated[Storage, Depends(get_storage)],
    config: Annotated[AppConfig, Depends(get_config)],
    checklist: Annotated[Checklist, Depends(get_checklist)],
) -> Response:
    form = parse_filters(request)
    # В списке нет реплик, а на листе «Расшифровки» они нужны — берём полные записи.
    full = [r for r in (storage.get(c.id) for c in storage.list_calls(form.filters)) if r]
    summary = build_summary(
        stats.to_call_results(full), [], checklist, config.thresholds, "панель CallScope"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "report.xlsx"
        write_xlsx(summary, path)
        data = path.read_bytes()
    name = f"callscope_{date.today():%Y-%m-%d}.xlsx"
    return Response(
        data,
        media_type=XLSX_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# --- страница звонка --------------------------------------------------------------------


def _record_or_404(storage: Storage, call_id: int) -> CallRecord:
    record = storage.get(call_id)
    if record is None:
        raise HTTPException(404, "Звонок не найден.")
    return record


@router.get("/calls/{call_id}", response_class=HTMLResponse)
def call_page(
    request: Request,
    call_id: int,
    storage: Annotated[Storage, Depends(get_storage)],
    config: Annotated[AppConfig, Depends(get_config)],
    checklist: Annotated[Checklist, Depends(get_checklist)],
) -> HTMLResponse:
    record = _record_or_404(storage, call_id)
    titles = {i.id: i.title for i in checklist.items}
    auto = {i.item_id: i for i in record.auto_items}
    context = {
        "page_title": record.file_name,
        "call": record,
        "titles": titles,
        "auto": auto,
        "thresholds": config.thresholds,
        "ready": record.status == "done" and record.score is not None,
        "status_label": {
            "queued": "в очереди",
            "processing": f"обрабатывается {record.progress * 100:.0f}%",
            "error": "ошибка обработки",
            "done": "готово",
        }[record.status],
        "when": record.call_datetime or record.uploaded_at,
    }
    return render(request, "call.html", context, active="calls")


def _back(call_id: int, anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/calls/{call_id}{anchor}", status_code=303)


@router.post("/calls/{call_id}/correction")
def save_correction(
    call_id: int,
    storage: Annotated[Storage, Depends(get_storage)],
    item_id: Annotated[str, Form()],
    status: Annotated[str, Form()] = "auto",
    comment: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "save",
) -> RedirectResponse:
    _record_or_404(storage, call_id)
    anchor = f"#item-{item_id}"
    comment = comment.strip()[:2000]
    if status not in ("auto", "passed", "failed", "na"):
        raise HTTPException(400, "Неизвестный статус пункта.")
    try:
        if action == "clear" or (status == "auto" and not comment):
            storage.clear_correction(call_id, item_id)
        else:
            chosen: Literal["passed", "failed", "na"] | None = (
                None if status == "auto" else status  # type: ignore[assignment]
            )
            storage.set_correction(call_id, item_id, chosen, comment)
    except CallScopeError as error:
        raise HTTPException(400, error.message_ru) from error
    return _back(call_id, anchor)


@router.post("/calls/{call_id}/swap")
def swap_sides(
    call_id: int,
    storage: Annotated[Storage, Depends(get_storage)],
    config: Annotated[AppConfig, Depends(get_config)],
    checklist: Annotated[Checklist, Depends(get_checklist)],
) -> RedirectResponse:
    _record_or_404(storage, call_id)
    try:
        rescoring.swap_sides(storage, call_id, config, checklist, _get_normalizer())
    except CallScopeError as error:
        raise HTTPException(409, error.message_ru) from error
    return _back(call_id)


@router.post("/calls/{call_id}/delete")
def delete_call(
    call_id: int, storage: Annotated[Storage, Depends(get_storage)]
) -> RedirectResponse:
    _record_or_404(storage, call_id)
    storage.delete(call_id)
    return RedirectResponse("/calls", status_code=303)
