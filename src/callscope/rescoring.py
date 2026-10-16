"""Пересчёты без расшифровки: новый чек-лист, смена сторон, новый шаблон имени файла.

Работают по сохранённой сырой расшифровке (`CallRecord.raw`); аудио и модель не нужны.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from callscope.checklist import Checklist
from callscope.config import AppConfig
from callscope.errors import CallScopeError
from callscope.filenames import parse_filename
from callscope.model import CallMeta
from callscope.pipeline import analyze_raw, is_template_warning, template_warning
from callscope.storage import Storage
from callscope.textnorm import Normalizer


def rescore_call(
    storage: Storage,
    call_id: int,
    config: AppConfig,
    checklist: Checklist,
    normalizer: Normalizer,
) -> None:
    """Переоценить звонок по сохранённой расшифровке. Менеджер и дата не меняются,
    выбранная сторона менеджера сохраняется, правки остаются (кроме удалённых пунктов)."""
    record = storage.get(call_id)
    if record is None:
        raise CallScopeError("Звонок не найден.")
    if record.raw is None:
        raise CallScopeError(
            f"У звонка {record.file_name} нет сохранённой расшифровки — переоценка невозможна."
        )
    result = analyze_raw(
        record.file_name,
        record.raw,
        config,
        checklist,
        normalizer,
        manager_channel=record.manager_channel,
        use_semantic=config.semantic.enabled,
    )
    meta = CallMeta(
        file_name=record.file_name,
        manager=record.manager,
        call_datetime=record.call_datetime,
        matched_template=record.matched_template,
    )
    # Предупреждение о шаблоне строится от сохранённых менеджера и даты, а не от нового
    # разбора имени: переоценка их не меняет.
    warnings = [w for w in result.warnings if not is_template_warning(w)]
    if not record.matched_template:
        warnings.insert(0, template_warning(record.file_name, config, record.manager))
    storage.update_analysis(
        call_id,
        replace(result, meta=meta, warnings=tuple(warnings)),
        checklist,
        config.thresholds,
        expected_channel=record.manager_channel,
    )


def rescore_all(
    storage: Storage,
    config: AppConfig,
    checklist: Checklist,
    normalizer: Normalizer,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Переоценить все готовые звонки; возвращает число переоценённых."""
    targets = [r.id for r in storage.list_calls() if r.status == "done"]
    total = len(targets)
    done = 0
    for index, call_id in enumerate(targets, start=1):
        record = storage.get(call_id)
        if record is not None and record.status == "done" and record.raw is not None:
            rescore_call(storage, call_id, config, checklist, normalizer)
            done += 1
        if on_progress is not None:
            on_progress(index, total)
    return done


def swap_sides(
    storage: Storage,
    call_id: int,
    config: AppConfig,
    checklist: Checklist,
    normalizer: Normalizer,
) -> None:
    """Поменять стороны менеджера и клиента и переоценить звонок без расшифровки."""
    record = storage.get(call_id)
    if record is None or record.dialogue is None:
        raise CallScopeError("Звонок не найден или ещё не обработан.")
    opposite = "right" if record.dialogue.manager_channel == "left" else "left"
    storage.set_manager_channel(call_id, opposite)
    rescore_call(storage, call_id, config, checklist, normalizer)


def reapply_template(storage: Storage, config: AppConfig) -> tuple[int, int]:
    """Разобрать исходные имена по новому шаблону. Возвращает (обновлено, не подошло);
    у не подошедших менеджер и дата остаются прежними."""
    updated = unmatched = 0
    for record in storage.list_calls():
        meta = parse_filename(
            record.file_name, config.filename_template, config.date_format, config.time_format
        )
        if meta.matched_template:
            # Имя теперь подошло — старое предупреждение о шаблоне больше не верно.
            warnings = tuple(w for w in record.warnings if not is_template_warning(w))
            storage.update_meta(record.id, meta, warnings)
            updated += 1
        else:
            unmatched += 1
    return updated, unmatched
