"""«Загрузка»: приём записей по одной, очередь со статусами, «Повторить», файлы записей."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from callscope.storage import CallRecord, Storage
from callscope.web.deps import format_score, get_storage, get_worker, render
from callscope.worker import Worker

AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".m4a")
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_MULTIPART_OVERHEAD = 64 * 1024
_CHUNK = 1 << 20

AUDIO_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}

STATUS_TEXT = {
    "queued": "в очереди",
    "processing": "обрабатывается",
    "done": "готово",
    "error": "ошибка",
}

router = APIRouter()


@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request) -> HTMLResponse:
    return render(
        request,
        "upload.html",
        {
            "page_title": "Загрузка записей",
            "extensions": ", ".join(e.lstrip(".") for e in AUDIO_EXTENSIONS),
            "max_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        },
        active="upload",
    )


def _rejected(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"id": None, "status": "rejected", "duplicate": False, "message": message},
        status_code=status_code,
    )


def _too_big(file_name: str) -> str:
    return f"{file_name}: файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ."


def _spool(upload: UploadFile, folder: Path) -> tuple[Path, int] | None:
    """Копирует загрузку во временный файл; None — превышен предел размера."""
    fd, name = tempfile.mkstemp(dir=folder, prefix=".incoming-", suffix=".tmp")
    path = Path(name)
    size = 0
    too_big = False
    try:
        with os.fdopen(fd, "wb") as out:
            upload.file.seek(0)
            while chunk := upload.file.read(_CHUNK):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    too_big = True
                    break
                out.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if too_big:
        path.unlink(missing_ok=True)
        return None
    return path, size


@router.post("/api/upload")
async def api_upload(
    request: Request,
    storage: Annotated[Storage, Depends(get_storage)],
    worker: Annotated[Worker | None, Depends(get_worker)],
) -> JSONResponse:
    """Одна запись на запрос (поле формы `file`) → `{id, status, duplicate, message, url}`."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_UPLOAD_BYTES + _MULTIPART_OVERHEAD:
        return _rejected(_too_big("Запись"), 413)
    form = await request.form(max_files=1, max_fields=5)
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.filename:
            return _rejected("Файл не передан.")
        file_name = Path(upload.filename.replace("\\", "/")).name
        if Path(file_name).suffix.lower() not in AUDIO_EXTENSIONS:
            allowed = ", ".join(e.lstrip(".") for e in AUDIO_EXTENSIONS)
            return _rejected(f"{file_name}: не тот формат — подходят {allowed}.", 415)
        spooled = await run_in_threadpool(_spool, upload, storage.data_dir)
        if spooled is None:
            return _rejected(_too_big(file_name), 413)
        path, size = spooled
        try:
            if size == 0:
                return _rejected(f"{file_name}: файл пустой.")
            record, is_new = await run_in_threadpool(storage.add_upload, file_name, path)
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
    finally:
        await form.close()

    if is_new and worker is not None:
        worker.wake()
    url = f"/calls/{record.id}"
    if is_new:
        message = f"{file_name}: принята, в очереди."
    else:
        message = f"{file_name}: уже загружена — звонок №{record.id}."
    return JSONResponse(
        {
            "id": record.id,
            "status": record.status,
            "duplicate": not is_new,
            "message": message,
            "url": url,
        },
        status_code=201 if is_new else 200,
    )


def queue_item(record: CallRecord) -> dict[str, Any]:
    """Строка очереди для `/api/queue` и страницы «Загрузка»."""
    percent = round(record.progress * 100)
    text = STATUS_TEXT[record.status]
    if record.status == "processing":
        text = f"обрабатывается {percent}%"
    score = record.score.score if record.score is not None else None
    return {
        "id": record.id,
        "file_name": record.file_name,
        "manager": record.manager,
        "status": record.status,
        "status_text": text,
        "progress": percent,
        "score": score,
        "score_text": format_score(score),
        "grade": record.score.grade if record.score is not None else None,
        "error": record.error,
        "url": f"/calls/{record.id}",
        "uploaded_at": record.uploaded_at.isoformat(),
    }


@router.get("/api/queue")
def api_queue(storage: Annotated[Storage, Depends(get_storage)]) -> dict[str, Any]:
    """Последние загрузки (новые сверху) и сколько в каждом статусе."""
    records = storage.queue_overview()
    counts = dict.fromkeys(STATUS_TEXT, 0)
    for record in records:
        counts[record.status] += 1
    return {"items": [queue_item(r) for r in records], "counts": counts}


@router.post("/api/calls/{call_id}/retry")
def api_retry(
    call_id: int,
    storage: Annotated[Storage, Depends(get_storage)],
    worker: Annotated[Worker | None, Depends(get_worker)],
) -> JSONResponse:
    record = storage.get(call_id)
    if record is None:
        return JSONResponse({"detail": "Звонок не найден."}, status_code=404)
    if record.status != "error":
        return JSONResponse(
            {"detail": "Повторить можно только запись с ошибкой."}, status_code=409
        )
    storage.retry(call_id)
    if worker is not None:
        worker.wake()
    return JSONResponse({"id": call_id, "status": "queued"})


@router.get("/audio/{call_id}")
def audio_file(call_id: int, storage: Annotated[Storage, Depends(get_storage)]) -> Response:
    """Файл записи; поддерживает Range (206) — без этого браузер не перематывает."""
    record = storage.get(call_id)
    if record is None or not record.audio_path.is_file():
        return JSONResponse({"detail": "Запись не найдена."}, status_code=404)
    suffix = Path(record.file_name).suffix.lower()
    return FileResponse(
        record.audio_path,
        media_type=AUDIO_MEDIA_TYPES.get(suffix, "application/octet-stream"),
        headers={"Cache-Control": "private, max-age=3600"},
    )
