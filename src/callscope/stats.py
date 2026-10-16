"""Статистика и фильтры — чистые функции над `CallRecord`.

Всё считается по ДЕЙСТВУЮЩИМ оценкам (`CallRecord.score`, с ручными правками) и только
по звонкам со статусом «готово». Дата звонка — `call_datetime`, иначе местная дата загрузки.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Literal

from callscope.checklist import Checklist
from callscope.config import Thresholds
from callscope.model import CallMeta, CallResult, CallScore, Dialogue

if TYPE_CHECKING:
    from callscope.storage import CallRecord

PresetName = Literal["week", "4weeks", "quarter", "all"]

_PRESET_DAYS: dict[str, tuple[int, str]] = {
    "week": (7, "Последние 7 дней"),
    "4weeks": (28, "Последние 4 недели"),
    "quarter": (90, "Последний квартал"),
}


@dataclass(frozen=True)
class Period:
    """Период, границы включительно; None — без границы."""

    start: date | None
    end: date | None
    label: str


@dataclass(frozen=True)
class CallFilters:
    period: Period
    manager: str | None = None
    score_min: float | None = None
    score_max: float | None = None
    failed_item: str | None = None


@dataclass(frozen=True)
class FunnelStep:
    item_id: str
    title: str
    failed: int  # звонков, где пункт провален
    lost: int  # звонков, потерянных на этом пункте (первый провал)


@dataclass(frozen=True)
class WeekPoint:
    week_start: date  # понедельник
    calls: int
    avg_score: float | None  # None — на неделе нет звонков


@dataclass(frozen=True)
class Overview:
    calls: int
    avg_score: float | None
    below_good_share: float | None
    good_threshold: float
    funnel: tuple[FunnelStep, ...]
    top_failure: FunnelStep | None
    weekly: tuple[WeekPoint, ...]


@dataclass(frozen=True)
class ManagerRow:
    manager: str
    calls: int
    avg_score: float
    top_failures: tuple[tuple[str, int], ...]


# --- период и фильтры -----------------------------------------------------------------


def period_preset(name: PresetName, today: date) -> Period:
    if name == "all":
        return Period(None, None, "Всё время")
    days, label = _PRESET_DAYS[name]
    return Period(today - timedelta(days=days - 1), today, label)


def period_custom(start: date | None, end: date | None) -> Period:
    """Свои даты; перепутанные границы меняются местами."""
    if start and end and start > end:
        start, end = end, start
    if start and end:
        label = f"{start:%d.%m.%Y} — {end:%d.%m.%Y}"
    elif start:
        label = f"С {start:%d.%m.%Y}"
    elif end:
        label = f"По {end:%d.%m.%Y}"
    else:
        label = "Всё время"
    return Period(start, end, label)


def call_date(record: CallRecord) -> date:
    """Дата звонка: из имени файла, иначе местная дата загрузки."""
    moment = record.call_datetime or record.uploaded_at.astimezone()
    return moment.date()


def apply_filters(calls: Iterable[CallRecord], filters: CallFilters) -> list[CallRecord]:
    """Только готовые звонки, прошедшие все заданные условия (по действующему баллу)."""
    start, end = filters.period.start, filters.period.end
    result: list[CallRecord] = []
    for record in calls:
        score = record.score
        if record.status != "done" or score is None:
            continue
        day = call_date(record)
        if (start and day < start) or (end and day > end):
            continue
        if filters.manager is not None and record.manager != filters.manager:
            continue
        if filters.score_min is not None and score.score < filters.score_min:
            continue
        if filters.score_max is not None and score.score > filters.score_max:
            continue
        if filters.failed_item is not None and not _failed(score, filters.failed_item):
            continue
        result.append(record)
    return result


def _failed(score: CallScore, item_id: str) -> bool:
    return any(i.item_id == item_id and i.status == "failed" for i in score.items)


def _done(calls: Iterable[CallRecord]) -> list[CallRecord]:
    return [c for c in calls if c.status == "done" and c.score is not None]


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


# --- обзор ----------------------------------------------------------------------------


def overview(
    calls: Iterable[CallRecord], checklist: Checklist, thresholds: Thresholds, period: Period
) -> Overview:
    chosen = apply_filters(calls, CallFilters(period))
    scores = [c.score for c in chosen if c.score is not None]
    values = [s.score for s in scores]

    failed: Counter[str] = Counter()
    lost: Counter[str] = Counter()
    for score in scores:
        for item in score.items:
            if item.status == "failed":
                failed[item.item_id] += 1
        if score.loss_item_id:
            lost[score.loss_item_id] += 1
    funnel = tuple(
        FunnelStep(i.id, i.title, failed[i.id], lost[i.id]) for i in checklist.items
    )
    # Самый частый провал; при равенстве — раньше в чек-листе.
    top = max(funnel, key=lambda s: s.failed, default=None)
    if top is not None and top.failed == 0:
        top = None

    below = sum(1 for v in values if v < thresholds.good)
    return Overview(
        calls=len(chosen),
        avg_score=_average(values),
        below_good_share=below / len(values) if values else None,
        good_threshold=thresholds.good,
        funnel=funnel,
        top_failure=top,
        weekly=tuple(weekly(chosen, period)),
    )


def weekly(calls: Iterable[CallRecord], period: Period) -> list[WeekPoint]:
    """Средний балл по неделям (с понедельника). Неделя без звонков — `avg_score=None`."""
    chosen = apply_filters(calls, CallFilters(period))
    by_week: dict[date, list[float]] = {}
    for record in chosen:
        day = call_date(record)
        monday = day - timedelta(days=day.weekday())
        if record.score is not None:
            by_week.setdefault(monday, []).append(record.score.score)
    first = period.start or (min(by_week) if by_week else None)
    last = period.end or (max(by_week) if by_week else None)
    if first is None or last is None:
        return []
    cursor = first - timedelta(days=first.weekday())
    points: list[WeekPoint] = []
    while cursor <= last:
        values = by_week.get(cursor, [])
        points.append(WeekPoint(cursor, len(values), _average(values)))
        cursor += timedelta(days=7)
    return points


# --- менеджеры ------------------------------------------------------------------------


def manager_ranking(calls: Iterable[CallRecord], checklist: Checklist) -> list[ManagerRow]:
    """Менеджеры по убыванию среднего балла; в `top_failures` — до трёх частых провалов."""
    order = {item.id: n for n, item in enumerate(checklist.items)}
    by_manager: dict[str, list[CallRecord]] = {}
    for record in _done(calls):
        by_manager.setdefault(record.manager, []).append(record)
    rows: list[ManagerRow] = []
    for manager, records in by_manager.items():
        failures: Counter[str] = Counter()
        for record in records:
            if record.score is not None:
                failures.update(i.item_id for i in record.score.items if i.status == "failed")
        ranked = sorted(failures.items(), key=lambda p: (-p[1], order.get(p[0], len(order))))
        values = [r.score.score for r in records if r.score is not None]
        rows.append(
            ManagerRow(manager, len(records), _average(values) or 0.0, tuple(ranked[:3]))
        )
    rows.sort(key=lambda r: (-r.avg_score, -r.calls, r.manager))
    return rows


# --- для отчётов этапа 1 --------------------------------------------------------------


def to_call_results(calls: Iterable[CallRecord]) -> list[CallResult]:
    """`CallResult` с действующими баллами — для `aggregate.build_summary` и `write_xlsx`."""
    empty = Dialogue((), False, "left", "manual")
    return [
        CallResult(
            meta=CallMeta(c.file_name, c.manager, c.call_datetime, c.matched_template),
            dialogue=c.dialogue or empty,
            score=c.score,
            warnings=c.warnings,
        )
        for c in _done(calls)
        if c.score is not None
    ]
