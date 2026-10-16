"""«Настройки» — редактор чек-листа, переоценка, шаблон имени файла."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from callscope.checklist import Checklist, ChecklistItem, parse_checklist_text, save_checklist
from callscope.config import AppConfig, save_config
from callscope.errors import CallScopeError, ConfigError
from callscope.filenames import parse_filename
from callscope.rescoring import reapply_template
from callscope.storage import Storage
from callscope.web.deps import (
    WorkingSettings,
    format_datetime,
    get_config,
    get_storage,
    get_worker,
    get_working_settings,
    render,
)
from callscope.worker import RescoreStatus, Worker

router = APIRouter()

# Поля пункта в форме, в порядке карточки. Каждое поле приходит списком — по одному
# значению на пункт, в порядке карточек на странице (так работают «вверх/вниз»).
ITEM_FIELDS = (
    "id",
    "title",
    "weight",
    "speaker",
    "phrases",
    "statement_phrases",
    "synonyms",
    "window",
    "window_size",
    "min_matches",
    "rule",
    "trigger",
    "respond_within",
    "description",
)
LINE_FIELDS = ("phrases", "statement_phrases", "trigger")
INT_FIELDS = ("window_size", "min_matches", "respond_within")

SPEAKER_OPTIONS = (("manager", "Менеджер"), ("client", "Клиент"))
WINDOW_OPTIONS = (
    ("all", "Весь звонок"),
    ("first", "Начало звонка"),
    ("last", "Конец звонка"),
)
RULE_OPTIONS = (
    ("phrases", "По ключевым фразам"),
    ("open_questions", "Открытые вопросы"),
)

DEFAULT_EXAMPLE = "ivanov_2026-10-12_1530.mp3"

_ERROR_TAIL = re.compile(r"поле «([^»]+)» — (.*?)\.?$", re.DOTALL)


# --- преобразования «текст в форме» <-> «данные чек-листа» ---------------------------


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def synonyms_to_text(synonyms: dict[str, tuple[str, ...]]) -> str:
    return "\n".join(f"{word}: {', '.join(repl)}" for word, repl in synonyms.items())


def parse_synonyms_text(text: str) -> tuple[dict[str, list[str]], str | None]:
    """«слово: замена, замена» по строке. Возвращает (синонимы, сообщение об ошибке)."""
    result: dict[str, list[str]] = {}
    for line in _lines(text):
        word, sep, tail = line.partition(":")
        replacements = [part.strip() for part in tail.split(",") if part.strip()]
        if not sep or not word.strip() or not replacements:
            return result, f"строка «{line}»: нужен вид «слово: замена, замена»"
        result[word.strip()] = replacements
    return result, None


def _number_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def item_view(item: ChecklistItem) -> dict[str, str]:
    """Пункт чек-листа -> строки для полей формы."""
    return {
        "id": item.id,
        "title": item.title,
        "weight": _number_text(item.weight),
        "speaker": item.speaker,
        "phrases": "\n".join(item.phrases),
        "statement_phrases": "\n".join(item.statement_phrases),
        "synonyms": synonyms_to_text(item.synonyms),
        "window": item.window,
        "window_size": str(item.window_size),
        "min_matches": str(item.min_matches),
        "rule": item.rule,
        "trigger": "\n".join(item.trigger),
        "respond_within": str(item.respond_within),
        "description": item.description,
    }


def blank_item_view() -> dict[str, str]:
    return {
        "id": "",
        "title": "",
        "weight": "10",
        "speaker": "manager",
        "phrases": "",
        "statement_phrases": "",
        "synonyms": "",
        "window": "all",
        "window_size": "3",
        "min_matches": "1",
        "rule": "phrases",
        "trigger": "",
        "respond_within": "2",
        "description": "",
    }


def _weight_value(text: str) -> Any:
    raw = text.strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return text.strip()  # валидатор скажет «нужно число больше нуля»
    return int(value) if value.is_integer() else value


def view_to_data(view: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    """Строки формы -> словарь пункта для YAML и ошибки полей, найденные по дороге."""
    errors: dict[str, str] = {}
    data: dict[str, Any] = {}
    if view["id"].strip():
        data["id"] = view["id"].strip()
    if view["title"].strip():
        data["title"] = view["title"].strip()
    if view["weight"].strip():
        data["weight"] = _weight_value(view["weight"])
    for name in ("speaker", "window", "rule"):
        data[name] = view[name]
    for name in INT_FIELDS:
        text = view[name].strip()
        if text:
            data[name] = int(text) if re.fullmatch(r"\d+", text) else text
    for name in LINE_FIELDS:
        lines = _lines(view[name])
        if lines:
            data[name] = lines
    synonyms, problem = parse_synonyms_text(view["synonyms"])
    if problem:
        errors["synonyms"] = problem
    elif synonyms:
        data["synonyms"] = synonyms
    if view["description"].strip():
        data["description"] = view["description"].strip()
    return data, errors


def _split_error(exc: ConfigError) -> tuple[str | None, str]:
    """Сообщение валидатора -> (поле, текст без служебного начала)."""
    match = _ERROR_TAIL.search(exc.message_ru)
    if match:
        return match.group(1), match.group(2).strip()
    return None, exc.message_ru


def _to_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def validate_form(
    views: list[dict[str, str]], global_synonyms_text: str
) -> tuple[Checklist | None, list[dict[str, str]], dict[str, str]]:
    """Проверяет форму. Возвращает (чек-лист или None, ошибки по пунктам, общие ошибки).

    Ошибки пункта: {имя поля: текст}; ключ `_` — ошибка пункта целиком.
    """
    item_errors: list[dict[str, str]] = [{} for _ in views]
    general: dict[str, str] = {}
    datas: list[dict[str, Any]] = []
    for index, view in enumerate(views):
        data, errors = view_to_data(view)
        item_errors[index].update(errors)
        datas.append(data)
        if errors:
            continue
        try:
            parse_checklist_text(_to_yaml({"items": [data]}))
        except ConfigError as exc:
            field, text = _split_error(exc)
            item_errors[index][field if field in ITEM_FIELDS else "_"] = text

    global_synonyms, problem = parse_synonyms_text(global_synonyms_text)
    if problem:
        general["synonyms"] = problem
    if not views:
        general["items"] = "Добавьте хотя бы один пункт."
    if general or any(item_errors):
        return None, item_errors, general
    doc: dict[str, Any] = {"items": datas}
    if global_synonyms:
        doc["synonyms"] = global_synonyms
    try:
        return parse_checklist_text(_to_yaml(doc)), item_errors, general
    except ConfigError as exc:
        general["items"] = _split_error(exc)[1]
        return None, item_errors, general


def _assign_new_ids(views: list[dict[str, str]]) -> None:
    """Новым пунктам (без id) выдаёт служебные id: item_1, item_2…"""
    taken = {view["id"].strip() for view in views if view["id"].strip()}
    counter = 0
    for view in views:
        if view["id"].strip():
            continue
        while f"item_{counter + 1}" in taken:
            counter += 1
        counter += 1
        view["id"] = f"item_{counter}"
        taken.add(view["id"])


# --- переоценка ----------------------------------------------------------------------


def rescore_payload(status: RescoreStatus | None) -> dict[str, Any]:
    if status is None:
        return {
            "state": "unavailable",
            "done": 0,
            "total": 0,
            "message": "Фоновая обработка сейчас не запущена — переоценка недоступна.",
            "finished_at": None,
            "error": None,
        }
    if status.state == "running":
        message = (
            f"Переоценка идёт: {status.done} из {status.total}"
            if status.total
            else "Переоценка начинается…"
        )
    elif status.state == "done":
        when = format_datetime(status.finished_at)
        if status.error:
            message = f"Переоценка прервана ({when}): {status.error}"
        else:
            message = f"Готово: переоценено {status.done} из {status.total} · завершено {when}"
    else:
        message = "Переоценка ещё не запускалась."
    return {
        "state": status.state,
        "done": status.done,
        "total": status.total,
        "message": message,
        "finished_at": status.finished_at.isoformat() if status.finished_at else None,
        "error": status.error,
    }


@router.get("/api/rescore")
def rescore_state(worker: Annotated[Worker | None, Depends(get_worker)]) -> JSONResponse:
    return JSONResponse(rescore_payload(worker.rescore_status if worker else None))


@router.post("/settings/rescore")
def rescore_start(request: Request, worker: Annotated[Worker | None, Depends(get_worker)]):
    wants_json = "application/json" in request.headers.get("accept", "")
    if worker is None:
        if wants_json:
            return JSONResponse(rescore_payload(None), status_code=503)
        return RedirectResponse("/settings/checklist?rescore=unavailable#rescore", status_code=303)
    if worker.rescore_status.state != "running":
        worker.request_rescore()
    if wants_json:
        return JSONResponse(rescore_payload(worker.rescore_status), status_code=202)
    return RedirectResponse("/settings/checklist#rescore", status_code=303)


# --- чек-лист ------------------------------------------------------------------------


@router.get("/settings")
def settings_root() -> RedirectResponse:
    return RedirectResponse("/settings/checklist", status_code=303)


def _checklist_context(
    request: Request,
    views: list[dict[str, str]],
    global_synonyms: str,
    *,
    item_errors: list[dict[str, str]] | None = None,
    general_errors: dict[str, str] | None = None,
    worker: Worker | None,
    saved: bool = False,
    notice: str | None = None,
) -> dict[str, Any]:
    return {
        "page_title": "Настройки — чек-лист",
        "tab": "checklist",
        "views": views,
        "item_errors": item_errors or [{} for _ in views],
        "general_errors": general_errors or {},
        "global_synonyms": global_synonyms,
        "blank": blank_item_view(),
        "rescore": rescore_payload(worker.rescore_status if worker else None),
        "saved": saved,
        "notice": notice,
        "speaker_options": SPEAKER_OPTIONS,
        "window_options": WINDOW_OPTIONS,
        "rule_options": RULE_OPTIONS,
    }


@router.get("/settings/checklist", response_class=HTMLResponse)
def checklist_page(
    request: Request,
    working: Annotated[WorkingSettings, Depends(get_working_settings)],
    worker: Annotated[Worker | None, Depends(get_worker)],
    saved: int = 0,
    rescore: str = "",
) -> Response:
    current = working.checklist()
    notice = (
        "Фоновая обработка не запущена, поэтому переоценить звонки сейчас нельзя."
        if rescore == "unavailable"
        else None
    )
    return render(
        request,
        "settings_checklist.html",
        _checklist_context(
            request,
            [item_view(item) for item in current.items],
            synonyms_to_text(current.synonyms),
            worker=worker,
            saved=bool(saved),
            notice=notice,
        ),
        active="settings",
    )


@router.post("/settings/checklist", response_class=HTMLResponse)
async def checklist_save(
    request: Request,
    storage: Annotated[Storage, Depends(get_storage)],
    worker: Annotated[Worker | None, Depends(get_worker)],
) -> Response:
    form = await request.form()
    columns = {name: [str(v) for v in form.getlist(name)] for name in ITEM_FIELDS}
    count = len(columns["title"])
    views = [
        {name: (columns[name][i] if i < len(columns[name]) else "") for name in ITEM_FIELDS}
        for i in range(count)
    ]
    for view in views:
        for name, default in (("speaker", "manager"), ("window", "all"), ("rule", "phrases")):
            view[name] = view[name] or default
    global_synonyms = str(form.get("global_synonyms", ""))
    _assign_new_ids(views)

    checklist, item_errors, general = validate_form(views, global_synonyms)
    if checklist is not None:
        try:
            save_checklist(checklist, storage.checklist_path)
        except CallScopeError as exc:
            general["items"] = exc.message_ru
            checklist = None
    if checklist is None:
        return render(
            request,
            "settings_checklist.html",
            _checklist_context(
                request,
                views,
                global_synonyms,
                item_errors=item_errors,
                general_errors=general,
                worker=worker,
            ),
            active="settings",
            status_code=400,
        )
    return RedirectResponse("/settings/checklist?saved=1", status_code=303)


# --- имя файла -----------------------------------------------------------------------


def check_example(
    template: str, date_format: str, time_format: str, example: str
) -> dict[str, Any]:
    """Разбор имени файла по введённым настройкам -> понятный результат для страницы."""
    example = example.strip()
    if not example:
        return {"matched": None, "message": "Введите пример имени файла, чтобы проверить шаблон."}
    meta = parse_filename(example, template, date_format, time_format)
    if not meta.matched_template:
        return {
            "matched": False,
            "message": "Имя не подошло под шаблон: менеджер не определён, дата не найдена. "
            "Проверьте разделители, формат даты и времени.",
        }
    when = "не указана в шаблоне"
    if meta.call_datetime is not None:
        pattern = "%d.%m.%Y %H:%M" if "{time}" in template else "%d.%m.%Y"
        when = meta.call_datetime.strftime(pattern)
    return {
        "matched": True,
        "manager": meta.manager,
        "when": when,
        "message": f"Менеджер: {meta.manager}. Дата звонка: {when}.",
    }


def _filename_problem(template: str, date_format: str, time_format: str) -> dict[str, str]:
    errors: dict[str, str] = {}
    if "{manager}" not in template and "{date}" not in template:
        errors["template"] = "В шаблоне нужен хотя бы один из кусков {manager} или {date}."
    if not template.strip():
        errors["template"] = "Введите шаблон."
    if not date_format.strip():
        errors["date_format"] = "Введите формат даты."
    if not time_format.strip():
        errors["time_format"] = "Введите формат времени."
    return errors


def _filename_context(
    storage: Storage,
    values: dict[str, str],
    *,
    errors: dict[str, str] | None = None,
    check: dict[str, Any] | None = None,
    result: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "page_title": "Настройки — имя файла",
        "tab": "filename",
        "values": values,
        "errors": errors or {},
        "check": check,
        "result": result,
        "calls_count": len(storage.list_calls()),
    }


def _example_default(storage: Storage) -> str:
    calls = storage.list_calls()
    return calls[0].file_name if calls else DEFAULT_EXAMPLE


@router.get("/settings/filename", response_class=HTMLResponse)
def filename_page(
    request: Request,
    config: Annotated[AppConfig, Depends(get_config)],
    storage: Annotated[Storage, Depends(get_storage)],
    updated: int | None = None,
    unmatched: int | None = None,
) -> Response:
    values = {
        "template": config.filename_template,
        "date_format": config.date_format,
        "time_format": config.time_format,
        "example": _example_default(storage),
    }
    result = {"updated": updated, "unmatched": unmatched or 0} if updated is not None else None
    check = check_example(
        values["template"], values["date_format"], values["time_format"], values["example"]
    )
    return render(
        request,
        "settings_filename.html",
        _filename_context(storage, values, check=check, result=result),
        active="settings",
    )


def _form_values(form: Any) -> dict[str, str]:
    return {
        name: str(form.get(name, "")).strip()
        for name in ("template", "date_format", "time_format", "example")
    }


@router.post("/settings/filename/check")
async def filename_check(request: Request) -> JSONResponse:
    values = _form_values(await request.form())
    return JSONResponse(
        check_example(
            values["template"], values["date_format"], values["time_format"], values["example"]
        )
    )


@router.post("/settings/filename", response_class=HTMLResponse)
async def filename_save(
    request: Request,
    config: Annotated[AppConfig, Depends(get_config)],
    storage: Annotated[Storage, Depends(get_storage)],
) -> Response:
    form = await request.form()
    values = _form_values(form)
    check = check_example(
        values["template"], values["date_format"], values["time_format"], values["example"]
    )
    errors = _filename_problem(values["template"], values["date_format"], values["time_format"])
    if form.get("action") == "check" or errors:
        return render(
            request,
            "settings_filename.html",
            _filename_context(storage, values, errors=errors, check=check),
            active="settings",
            status_code=400 if errors else 200,
        )
    new_config = replace(
        config,
        filename_template=values["template"],
        date_format=values["date_format"],
        time_format=values["time_format"],
    )
    try:
        save_config(new_config, storage.config_path)
        updated, unmatched = reapply_template(storage, new_config)
    except CallScopeError as exc:
        return render(
            request,
            "settings_filename.html",
            _filename_context(storage, values, errors={"_": exc.message_ru}, check=check),
            active="settings",
            status_code=400,
        )
    return RedirectResponse(
        f"/settings/filename?updated={updated}&unmatched={unmatched}", status_code=303
    )
