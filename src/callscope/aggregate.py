"""Сводка по набору оценённых звонков."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from callscope.checklist import Checklist
from callscope.config import Thresholds
from callscope.model import CallResult, SkippedFile


@dataclass(frozen=True)
class ManagerSummary:
    manager: str
    calls: int
    avg_score: float
    top_failures: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Summary:
    generated_at: datetime
    source_dir: str
    checklist: Checklist
    thresholds: Thresholds
    calls: tuple[CallResult, ...]
    skipped: tuple[SkippedFile, ...]
    managers: tuple[ManagerSummary, ...]
    loss_distribution: tuple[tuple[str, int], ...]
    failure_ranking: tuple[tuple[str, int], ...]
    conclusion: str
    avg_score: float


def _failed_ids(call: CallResult) -> list[str]:
    return [r.item_id for r in call.score.items if r.status == "failed"]


def _ranked(counter: Counter[str], order: dict[str, int], limit: int | None = None):
    pairs = sorted(counter.items(), key=lambda p: (-p[1], order.get(p[0], 10**6)))
    return tuple(pairs[:limit] if limit else pairs)


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _conclusion(
    total: int, loss: tuple[tuple[str, int], ...], checklist: Checklist
) -> str:
    if total == 0:
        return "Нет оценённых звонков — выводов сделать нельзя."
    best = max(loss, key=lambda p: p[1], default=None)  # первый максимум — по порядку чек-листа
    if best is None or best[1] == 0:
        return "Потерь не найдено: во всех звонках выполнены все применимые пункты чек-листа."
    titles = {i.id: i.title for i in checklist.items}
    title = titles.get(best[0], best[0])
    return f"Чаще всего звонки теряются на этапе «{title}» — {best[1]} из {total}."


def build_summary(
    calls: list[CallResult],
    skipped: list[SkippedFile],
    checklist: Checklist,
    thresholds: Thresholds,
    source_dir: str,
    generated_at: datetime | None = None,
) -> Summary:
    order = {item.id: n for n, item in enumerate(checklist.items)}

    by_manager: dict[str, list[CallResult]] = {}
    for call in calls:
        by_manager.setdefault(call.meta.manager, []).append(call)

    managers = []
    for name, group in by_manager.items():
        fails: Counter[str] = Counter()
        for call in group:
            fails.update(_failed_ids(call))
        managers.append(
            ManagerSummary(
                manager=name,
                calls=len(group),
                avg_score=_avg([c.score.score for c in group]),
                top_failures=_ranked(fails, order, 3),
            )
        )
    managers.sort(key=lambda m: -m.avg_score)

    losses = Counter(c.score.loss_item_id for c in calls if c.score.loss_item_id)
    loss_distribution = tuple((item.id, losses.get(item.id, 0)) for item in checklist.items)

    all_fails: Counter[str] = Counter()
    for call in calls:
        all_fails.update(_failed_ids(call))

    return Summary(
        generated_at=generated_at or datetime.now(),
        source_dir=source_dir,
        checklist=checklist,
        thresholds=thresholds,
        calls=tuple(calls),
        skipped=tuple(skipped),
        managers=tuple(managers),
        loss_distribution=loss_distribution,
        failure_ranking=_ranked(all_fails, order),
        conclusion=_conclusion(len(calls), loss_distribution, checklist),
        avg_score=_avg([c.score.score for c in calls]),
    )
