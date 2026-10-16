"""Общий конвейер обработки одной записи: расшифровка (с кэшем) и оценка по чек-листу.

Им пользуются и `callscope analyze`, и очередь веб-панели. Без typer/rich и без печати.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from callscope.asr import Transcriber
from callscope.cache import TranscriptCache
from callscope.checklist import Checklist
from callscope.config import AppConfig
from callscope.filenames import parse_filename
from callscope.model import CallResult, RawTranscript
from callscope.roles import assign_roles
from callscope.scoring import score_call
from callscope.semantic import recheck
from callscope.textnorm import Normalizer


def transcript_for(
    audio: Path,
    transcriber: Transcriber,
    cache: TranscriptCache | None,
    config: AppConfig,
    on_progress: Callable[[float], None] | None = None,
) -> tuple[RawTranscript, bool]:
    """Сырая расшифровка записи и признак «взята из кэша».

    Ключ кэша учитывает модель (`config.model_path` или `config.model_size`) и порог пауз.
    `cache=None` — без кэша. Ошибка записи в кэш не мешает анализу.
    """
    key: str | None = None
    if cache is not None:
        key = cache.key(audio, config.model_path or config.model_size, config.mono_turn_gap)
        cached = cache.get(key)
        if cached is not None:
            return cached, True
    raw = transcriber.transcribe(audio, config.mono_turn_gap, on_progress=on_progress)
    if cache is not None and key is not None:
        try:
            cache.put(key, raw)
        except OSError:
            pass  # кэш — ускорение, без него анализ всё равно идёт
    return raw, False


# Общая метка предупреждения о шаблоне имени: по ней пересчёты находят и заменяют его.
TEMPLATE_WARNING_MARK = "имя не подходит под шаблон"


def template_warning(file_name: str, config: AppConfig, manager: str) -> str:
    """Предупреждение «имя файла не подошло под шаблон» с менеджером, который записан."""
    return (
        f"{file_name}: {TEMPLATE_WARNING_MARK} «{config.filename_template}» — "
        f"менеджер «{manager}»."
    )


def is_template_warning(text: str) -> bool:
    return TEMPLATE_WARNING_MARK in text


def analyze_raw(
    file_name: str,
    raw: RawTranscript,
    config: AppConfig,
    checklist: Checklist,
    normalizer: Normalizer,
    manager_channel: Literal["auto", "left", "right"] | None = None,
    use_semantic: bool = False,
) -> CallResult:
    """Имя файла → менеджер и дата; сырая расшифровка → диалог по ролям → оценка."""
    meta = parse_filename(
        file_name, config.filename_template, config.date_format, config.time_format
    )
    warnings: list[str] = []
    if not meta.matched_template:
        warnings.append(template_warning(file_name, config, meta.manager))
    dialogue, role_warnings = assign_roles(raw, config, file_name, manager_channel)
    warnings.extend(role_warnings)
    score = score_call(dialogue, checklist, config.thresholds, normalizer)
    if use_semantic:
        score, semantic_warnings = recheck(dialogue, checklist, score, config, config.thresholds)
        warnings.extend(semantic_warnings)
    return CallResult(meta=meta, dialogue=dialogue, score=score, warnings=tuple(warnings))
