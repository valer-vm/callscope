"""Разбор имени файла записи по шаблону из конфига."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import PurePath

from callscope.model import CallMeta

UNDEFINED_MANAGER = "Не определён"

_PLACEHOLDER = re.compile(r"\{(manager|date|time|any)\}")
_GROUP_PATTERNS = {
    "manager": r"(?P<manager>(?:[^\W_]|-)+)",
    "date": r"(?P<date>.+?)",
    "time": r"(?P<time>.+?)",
    "any": r".*?",
}


def _template_regex(template: str) -> re.Pattern[str]:
    parts: list[str] = []
    pos = 0
    used: set[str] = set()
    for match in _PLACEHOLDER.finditer(template):
        parts.append(re.escape(template[pos : match.start()]))
        name = match.group(1)
        if name != "any" and name in used:
            # повтор плейсхолдера: второе вхождение должно совпасть с первым
            parts.append(f"(?P={name})")
        else:
            parts.append(_GROUP_PATTERNS[name])
            used.add(name)
        pos = match.end()
    parts.append(re.escape(template[pos:]))
    return re.compile("".join(parts))


def _unmatched(file_name: str) -> CallMeta:
    return CallMeta(
        file_name=file_name, manager=UNDEFINED_MANAGER, call_datetime=None, matched_template=False
    )


def parse_filename(file_name: str, template: str, date_format: str, time_format: str) -> CallMeta:
    """Достаёт менеджера и дату-время звонка из имени файла; расширение отбрасывается."""
    stem = PurePath(file_name).stem
    match = _template_regex(template).fullmatch(stem)
    if match is None:
        return _unmatched(file_name)
    groups = match.groupdict()
    try:
        call_date: date | None = (
            datetime.strptime(groups["date"], date_format).date() if groups.get("date") else None
        )
        call_time: time | None = (
            datetime.strptime(groups["time"], time_format).time() if groups.get("time") else None
        )
    except ValueError:
        return _unmatched(file_name)
    call_datetime = (
        datetime.combine(call_date, call_time or time()) if call_date is not None else None
    )
    manager = groups.get("manager") or UNDEFINED_MANAGER
    return CallMeta(
        file_name=file_name, manager=manager, call_datetime=call_datetime, matched_template=True
    )
