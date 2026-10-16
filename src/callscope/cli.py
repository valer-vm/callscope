"""Команда `callscope analyze`: папка записей → отчёты HTML и Excel."""

from __future__ import annotations

import os
import time
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

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
from callscope.filenames import parse_filename
from callscope.model import CallResult, RawTranscript, SkippedFile
from callscope.report_html import write_html
from callscope.report_xlsx import write_xlsx
from callscope.roles import assign_roles
from callscope.scoring import score_call
from callscope.semantic import recheck
from callscope.textnorm import Normalizer

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

    transcriber = Transcriber(model_size, model_path)
    cache = TranscriptCache(path / CACHE_DIR_NAME)
    cache_label = model_path or model_size
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
                raw, from_cache = _transcript(
                    audio, transcriber, cache, cache_label, config, no_cache, on_progress
                )
            except AudioError as exc:
                skipped.append(SkippedFile(audio.name, exc.message_ru))
                progress.console.print(f"[yellow]Пропущен {audio.name}: {exc.message_ru}[/yellow]")
                progress.remove_task(current)
                progress.advance(overall)
                continue
            except CallScopeError as exc:
                progress.stop()
                raise _fail(exc.message_ru, 1) from None

            result = _analyze_one(
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


def _transcript(
    audio: Path,
    transcriber: Transcriber,
    cache: TranscriptCache,
    cache_label: str,
    config: AppConfig,
    no_cache: bool,
    on_progress,
) -> tuple[RawTranscript, bool]:
    key = cache.key(audio, cache_label, config.mono_turn_gap)
    if not no_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached, True
    raw = transcriber.transcribe(audio, config.mono_turn_gap, on_progress=on_progress)
    try:
        cache.put(key, raw)
    except OSError:
        pass  # кэш — ускорение, без него анализ всё равно идёт
    return raw, False


def _analyze_one(
    file_name: str,
    raw: RawTranscript,
    config: AppConfig,
    checklist: Checklist,
    normalizer: Normalizer,
    cli_channel: str,
    use_semantic: bool,
) -> CallResult:
    meta = parse_filename(
        file_name, config.filename_template, config.date_format, config.time_format
    )
    warnings: list[str] = []
    if not meta.matched_template:
        warnings.append(
            f"{file_name}: имя не подходит под шаблон «{config.filename_template}» — "
            f"менеджер «{meta.manager}»."
        )
    dialogue, role_warnings = assign_roles(raw, config, file_name, cli_channel)  # type: ignore[arg-type]
    warnings.extend(role_warnings)
    score = score_call(dialogue, checklist, config.thresholds, normalizer)
    if use_semantic:
        score, semantic_warnings = recheck(dialogue, checklist, score, config, config.thresholds)
        warnings.extend(semantic_warnings)
    return CallResult(meta=meta, dialogue=dialogue, score=score, warnings=tuple(warnings))
