"""Команды CallScope: `analyze` (папка записей → отчёты HTML и Excel), `serve` (веб-панель),
`import` (папка записей → очередь панели)."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from callscope.aggregate import build_summary
from callscope.asr import Transcriber
from callscope.cache import TranscriptCache
from callscope.checklist import Checklist, load_checklist
from callscope.config import AppConfig, load_config
from callscope.errors import AudioError, CallScopeError, ConfigError
from callscope.model import CallResult, RawTranscript, SkippedFile
from callscope.pipeline import analyze_raw, transcript_for
from callscope.report_html import write_html
from callscope.report_xlsx import write_xlsx
from callscope.textnorm import Normalizer

if TYPE_CHECKING:
    from fastapi import FastAPI

AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".m4a")
CACHE_DIR_NAME = ".callscope-cache"
DEFAULT_OUT = Path("reports")

app = typer.Typer(
    help="CallScope — анализ звонков по чек-листу скрипта продаж. Записи не покидают компьютер.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
console = Console(stderr=True, highlight=False)


class ManagerChannel(StrEnum):
    auto = "auto"
    left = "left"
    right = "right"


@app.callback()
def main() -> None:
    """CallScope — локальная речевая аналитика звонков."""


def _fail(message: str, code: int) -> typer.Exit:
    console.print(f"[red]{message}[/red]")
    return typer.Exit(code=code)


def find_recordings(folder: Path) -> list[Path]:
    """mp3/wav/ogg/m4a в папке без вложенных; регистр расширения не важен."""
    return sorted(
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )


def report_folder(base: Path, now: datetime) -> Path:
    """`<base>/<ГГГГ-ММ-ДД_ЧЧММ>`; если папка уже есть — суффикс `_2`, `_3`…"""
    stamp = now.strftime("%Y-%m-%d_%H%M")
    candidate = base / stamp
    number = 2
    while candidate.exists():
        candidate = base / f"{stamp}_{number}"
        number += 1
    return candidate


def out_problem(out: Path) -> str | None:
    """Почему в `out` не получится записать отчёт; None — можно писать.
    Папки может ещё не быть — проверяется ближайшая существующая."""
    existing = out
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    if not existing.is_dir():
        return f"{existing} — не папка, отчёт в {out} не записать."
    if not os.access(existing, os.W_OK | os.X_OK):
        return f"Нет прав на запись в папку {existing}: отчёт в {out} не записать."
    return None


class _RefreshCache(TranscriptCache):
    """--no-cache: кэш не читается, но свежая расшифровка в него записывается."""

    def get(self, key: str) -> RawTranscript | None:
        return None


def _load_settings(
    config_path: Path | None, checklist_path: Path | None
) -> tuple[AppConfig, Checklist]:
    config = load_config(config_path)
    if checklist_path is None:
        local = Path.cwd() / "checklist.yaml"
        checklist_path = local if local.is_file() else None
    return config, load_checklist(checklist_path)


@app.command()
def analyze(
    path: Annotated[Path, typer.Argument(help="Папка с записями (mp3, wav, ogg, m4a).")],
    out: Annotated[
        Path,
        typer.Option("--out", help="Куда складывать отчёты; внутри создаётся папка с датой."),
    ] = DEFAULT_OUT,
    config_path: Annotated[
        Path | None, typer.Option("--config", help="Файл callscope.yaml.")
    ] = None,
    checklist_path: Annotated[
        Path | None, typer.Option("--checklist", help="Файл checklist.yaml.")
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Размер модели: tiny, base, small, medium, large-v3."),
    ] = None,
    manager_channel: Annotated[
        ManagerChannel,
        typer.Option("--manager-channel", help="Сторона менеджера в стерео-записи."),
    ] = ManagerChannel.auto,
    semantic: Annotated[
        bool | None,
        typer.Option(
            "--semantic/--no-semantic", help="Перепроверить провалы локальной моделью Ollama."
        ),
    ] = None,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Расшифровать заново, без кэша.")
    ] = False,
) -> None:
    """Проанализировать папку с записями звонков и построить отчёты."""
    try:
        config, checklist = _load_settings(config_path, checklist_path)
    except ConfigError as exc:
        raise _fail(exc.message_ru, 2) from None

    if not path.exists():
        raise _fail(f"Папка {path} не найдена.", 2)
    if not path.is_dir():
        raise _fail(f"{path} — не папка. Укажите папку с записями.", 2)
    recordings = find_recordings(path)
    if not recordings:
        raise _fail(f"В папке {path} нет записей (mp3, wav, ogg, m4a).", 1)
    if problem := out_problem(out):
        raise _fail(problem + " Укажите другую папку в --out.", 2)

    model_size = model or config.model_size
    model_path = None if model else config.model_path
    use_semantic = config.semantic.enabled if semantic is None else semantic
    cli_channel = manager_channel.value

    # Ключ кэша считается по модели из настроек — подставляем выбранную в --model.
    config = replace(config, model_size=model_size, model_path=model_path)
    transcriber = Transcriber(model_size, model_path)
    cache_dir = path / CACHE_DIR_NAME
    cache = _RefreshCache(cache_dir) if no_cache else TranscriptCache(cache_dir)
    normalizer = Normalizer()
    thresholds = config.thresholds

    results: list[CallResult] = []
    skipped: list[SkippedFile] = []
    started = time.monotonic()
    console.print(f"Найдено записей: {len(recordings)}. Модель: {model_size}.")

    progress = Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )
    with progress:
        overall = progress.add_task("Файлы", total=len(recordings))
        for audio in recordings:
            current = progress.add_task(f"  {audio.name}", total=100)

            def on_progress(fraction: float, task_id=current) -> None:
                progress.update(task_id, completed=round(fraction * 100))

            try:
                raw, from_cache = transcript_for(audio, transcriber, cache, config, on_progress)
            except AudioError as exc:
                skipped.append(SkippedFile(audio.name, exc.message_ru))
                progress.console.print(f"[yellow]Пропущен {audio.name}: {exc.message_ru}[/yellow]")
                progress.remove_task(current)
                progress.advance(overall)
                continue
            except CallScopeError as exc:
                progress.stop()
                raise _fail(exc.message_ru, 1) from None

            result = analyze_raw(
                audio.name, raw, config, checklist, normalizer, cli_channel, use_semantic
            )
            results.append(result)
            source = "из кэша" if from_cache else "расшифрован"
            progress.console.print(
                f"{audio.name}: {source}; менеджер {result.meta.manager}, "
                f"балл {result.score.score:.0f}"
            )
            for warning in result.warnings:
                progress.console.print(f"[yellow]  {warning}[/yellow]")
            progress.remove_task(current)
            progress.advance(overall)

    if not results:
        reasons = "\n".join(f"  {s.file_name}: {s.reason}" for s in skipped)
        raise _fail(f"Ни один файл не удалось обработать, отчёт не создан:\n{reasons}", 1)

    summary = build_summary(results, skipped, checklist, thresholds, str(path))
    folder = report_folder(out, summary.generated_at)
    try:
        write_html(summary, folder / "report.html")
        write_xlsx(summary, folder / "report.xlsx")
    except OSError as exc:
        reason = "нет прав на запись" if isinstance(exc, PermissionError) else exc.strerror
        raise _fail(f"Не удалось записать отчёт в {folder}: {reason}.", 2) from None

    elapsed = time.monotonic() - started
    console.print(
        f"Готово за {elapsed:.0f} с: обработано {len(results)}, пропущено {len(skipped)}. "
        f"Средний балл {summary.avg_score:.0f}."
    )
    console.print(summary.conclusion)
    typer.echo(f"Отчёт: {folder}")


# --- веб-панель -----------------------------------------------------------------------


def _run_server(app_: FastAPI, host: str, port: int) -> None:
    import uvicorn

    # proxy_headers=False: адрес клиента (для блокировки перебора паролей) не берётся
    # из X-Forwarded-For — иначе его подделка с localhost обходит блокировку.
    uvicorn.run(app_, host=host, port=port, log_level="info", proxy_headers=False)


@app.command()
def serve(
    host: Annotated[
        str | None, typer.Option("--host", help="Адрес (по умолчанию 127.0.0.1).")
    ] = None,
    port: Annotated[int | None, typer.Option("--port", help="Порт (по умолчанию 8000).")] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Папка данных панели (по умолчанию ./callscope-data)."),
    ] = None,
    demo: Annotated[
        bool, typer.Option("--demo", help="Открыть демо-базу ./callscope-data-demo.")
    ] = False,
) -> None:
    """Запустить веб-панель руководителя (пароль — CALLSCOPE_PASSWORD в .env)."""
    from callscope.storage import DB_FILE_NAME, Storage
    from callscope.web import create_app
    from callscope.web.deps import WorkingSettings
    from callscope.web.settings import (
        load_env,
        load_web_settings,
        resolve_data_dir,
        server_address,
    )
    from callscope.worker import Worker

    load_env()
    folder = resolve_data_dir(data_dir, demo)
    if demo and not (folder / DB_FILE_NAME).is_file():
        raise _fail(
            f"Демо-база не найдена в {folder}. Создайте её командой: callscope demo", 2
        )
    try:
        settings = load_web_settings(folder, demo)
        bind_host, bind_port = server_address(host, port)
    except ConfigError as exc:
        raise _fail(exc.message_ru, 2) from None

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    storage = Storage(settings.data_dir)
    working = WorkingSettings(storage)
    worker = Worker(
        storage,
        lambda config: Transcriber(config.model_size, config.model_path),
        working.load,
    )
    web_app = create_app(settings, worker)
    shown_host = "127.0.0.1" if bind_host in ("0.0.0.0", "") else bind_host
    mode = " (демо)" if demo else ""
    console.print(f"Папка данных: {settings.data_dir.resolve()}{mode}")
    typer.echo(f"Панель: http://{shown_host}:{bind_port}")
    _run_server(web_app, bind_host, bind_port)


@app.command()
def demo(
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Папка демо-базы (по умолчанию ./callscope-data-demo)."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Пересоздать уже существующую демо-базу.")
    ] = False,
) -> None:
    """Создать демо-базу: 5 менеджеров и ~150 звонков за последние 8 недель."""
    from callscope.demo import build_demo, clear_demo, demo_exists
    from callscope.web.settings import resolve_data_dir

    folder = resolve_data_dir(data_dir, demo=True)
    if demo_exists(folder):
        if not force:
            raise _fail(
                f"Демо-база уже есть в {folder}. Чтобы создать заново: callscope demo --force",
                2,
            )
        if folder.resolve() == resolve_data_dir(None, demo=False).resolve():
            raise _fail(
                f"{folder} — рабочая папка данных, её нельзя пересоздавать как демо. "
                "Укажите другую папку в --data-dir.",
                2,
            )
        clear_demo(folder)
    try:
        summary = build_demo(folder)
    except (OSError, CallScopeError) as exc:
        message = exc.message_ru if isinstance(exc, CallScopeError) else str(exc)
        raise _fail(f"Не удалось создать демо-базу в {folder}: {message}", 2) from None
    typer.echo(
        f"Демо-база готова: {folder} — звонков {summary.calls} "
        f"(с аудио {summary.audio_calls}), менеджеров {len(summary.managers)}."
    )
    typer.echo("Открыть: callscope serve --demo")


@app.command("import")
def import_folder(
    path: Annotated[Path, typer.Argument(help="Папка с записями (mp3, wav, ogg, m4a).")],
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Папка данных панели (по умолчанию ./callscope-data)."),
    ] = None,
) -> None:
    """Поставить записи папки в очередь панели; уже загруженные пропускаются."""
    from callscope.storage import Storage
    from callscope.web.settings import load_env, resolve_data_dir
    from callscope.worker import CACHE_DIR_NAME as PANEL_CACHE_DIR

    if not path.exists():
        raise _fail(f"Папка {path} не найдена.", 2)
    if not path.is_dir():
        raise _fail(f"{path} — не папка. Укажите папку с записями.", 2)
    recordings = find_recordings(path)
    if not recordings:
        raise _fail(f"В папке {path} нет записей (mp3, wav, ogg, m4a).", 1)

    load_env()
    storage = Storage(resolve_data_dir(data_dir, demo=False))
    try:
        config = load_config(storage.config_path)
    except ConfigError as exc:
        raise _fail(exc.message_ru, 2) from None
    stage1_dir = path / CACHE_DIR_NAME
    stage1 = TranscriptCache(stage1_dir) if stage1_dir.is_dir() else None
    panel_cache = TranscriptCache(storage.data_dir / PANEL_CACHE_DIR)
    model = config.model_path or config.model_size

    queued = known = from_cache = 0
    for audio in recordings:
        try:
            _record, is_new = storage.add_upload(audio.name, audio)
        except OSError as exc:
            console.print(f"[yellow]Пропущен {audio.name}: {exc.strerror}[/yellow]")
            continue
        if not is_new:
            known += 1
            continue
        queued += 1
        if stage1 is not None and _copy_cached(audio, stage1, panel_cache, model, config):
            from_cache += 1

    typer.echo(f"Записи из {path}: поставлено {queued}, уже были {known}.")
    if stage1 is not None:
        typer.echo(f"Готовых расшифровок взято из кэша: {from_cache} (папка {CACHE_DIR_NAME}).")
    if queued:
        typer.echo("Обработает запущенная панель (callscope serve) или следующий её запуск.")


def _copy_cached(
    audio: Path, source: TranscriptCache, target: TranscriptCache, model: str, config: AppConfig
) -> bool:
    """Расшифровка этапа 1 → кэш панели (ключ тот же: содержимое + модель + порог пауз)."""
    try:
        key = source.key(audio, model, config.mono_turn_gap)
    except AudioError:
        return False
    raw = source.get(key)
    if raw is None:
        return False
    try:
        target.put(key, raw)
    except OSError:
        return False
    return True
