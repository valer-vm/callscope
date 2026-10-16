"""Общие подготовленные данные для HTML и Excel отчётов."""

from __future__ import annotations

from callscope.aggregate import Summary
from callscope.model import Grade, Role

GRADE_LABEL: dict[Grade, str] = {"good": "Хороший", "medium": "Средний", "bad": "Плохой"}
STATUS_LABEL = {"passed": "Выполнен", "failed": "Провален", "na": "Не применим"}
ROLE_LABEL: dict[Role, str] = {"manager": "Менеджер", "client": "Клиент"}


def fmt_time(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def titles(summary: Summary) -> dict[str, str]:
    return {item.id: item.title for item in summary.checklist.items}


def fmt_datetime(value) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else "—"
