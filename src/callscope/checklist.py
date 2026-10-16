"""Загрузка и проверка checklist.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from callscope.config import DEFAULTS_DIR, read_yaml_file
from callscope.errors import ConfigError
from callscope.model import Role

CHECKLIST_FILE_NAME = "checklist.yaml"

_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_ITEM_FIELDS = {
    "id",
    "title",
    "weight",
    "speaker",
    "phrases",
    "synonyms",
    "window",
    "window_size",
    "min_matches",
    "rule",
    "trigger",
    "respond_within",
    "description",
}


@dataclass(frozen=True)
class ChecklistItem:
    id: str
    title: str
    weight: float
    speaker: Role = "manager"
    phrases: tuple[str, ...] = ()
    synonyms: dict[str, tuple[str, ...]] = field(default_factory=dict)
    window: Literal["all", "first", "last"] = "all"
    window_size: int = 3
    min_matches: int = 1
    rule: Literal["phrases", "open_questions"] = "phrases"
    trigger: tuple[str, ...] = ()
    respond_within: int = 2
    description: str = ""


@dataclass(frozen=True)
class Checklist:
    items: tuple[ChecklistItem, ...]
    synonyms: dict[str, tuple[str, ...]] = field(default_factory=dict)


class _ItemErrors:
    def __init__(self, path: Path, label: str) -> None:
        self.path = path
        self.label = label

    def fail(self, field_name: str, problem: str) -> ConfigError:
        return ConfigError(
            f"Ошибка в {self.path.name}: пункт {self.label}, поле «{field_name}» — {problem}."
        )

    def text(self, data: dict[str, Any], name: str, default: str | None = None) -> str:
        value = data.get(name, default)
        if not isinstance(value, str) or not value.strip():
            raise self.fail(name, "нужна непустая строка")
        return value.strip()

    def positive_int(self, data: dict[str, Any], name: str, default: int) -> int:
        value = data.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise self.fail(name, "нужно целое число не меньше 1")
        return value

    def choice(
        self, data: dict[str, Any], name: str, default: str, options: tuple[str, ...]
    ) -> Any:
        value = data.get(name, default)
        if value not in options:
            raise self.fail(name, "допустимые значения: " + ", ".join(options))
        return value

    def phrase_list(self, data: dict[str, Any], name: str) -> tuple[str, ...]:
        value = data.get(name, [])
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise self.fail(name, "нужен список непустых строк")
        return tuple(v.strip() for v in value)


def parse_synonyms(
    value: Any, fail: Any, field_name: str = "synonyms"
) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise fail(field_name, "нужен набор «слово: [замены]»")
    result: dict[str, tuple[str, ...]] = {}
    for word, replacements in value.items():
        if isinstance(replacements, str):
            replacements = [replacements]
        if (
            not isinstance(word, str)
            or not word.strip()
            or not isinstance(replacements, list)
            or not all(isinstance(r, str) and r.strip() for r in replacements)
        ):
            raise fail(field_name, f"у слова «{word}» должен быть список замен-строк")
        result[word.strip()] = tuple(r.strip() for r in replacements)
    return result


def _parse_item(path: Path, number: int, data: Any, seen: set[str]) -> ChecklistItem:
    if not isinstance(data, dict):
        raise ConfigError(f"Ошибка в {path.name}: пункт №{number} должен быть набором полей.")
    raw_id = data.get("id")
    title = data.get("title")
    label = f"№{number}"
    if isinstance(raw_id, str) and raw_id:
        label = f"«{raw_id}»"
    if isinstance(title, str) and title:
        label += f" ({title})"
    errors = _ItemErrors(path, label)

    for key in data:
        if key not in _ITEM_FIELDS:
            raise errors.fail(str(key), "неизвестное поле")

    item_id = errors.text(data, "id")
    if not _ID.match(item_id):
        raise errors.fail("id", "только латинские строчные буквы, цифры и «_», начиная с буквы")
    if item_id in seen:
        raise errors.fail("id", f"пункт с id «{item_id}» уже есть")
    seen.add(item_id)

    if "weight" not in data:
        raise errors.fail("weight", "не указан вес")
    weight = data["weight"]
    if isinstance(weight, bool) or not isinstance(weight, int | float) or weight <= 0:
        raise errors.fail("weight", "нужно число больше нуля")

    phrases = errors.phrase_list(data, "phrases")
    if not phrases:
        raise errors.fail("phrases", "нужна хотя бы одна ключевая фраза")

    description = data.get("description", "")
    if not isinstance(description, str):
        raise errors.fail("description", "нужна строка")

    return ChecklistItem(
        id=item_id,
        title=errors.text(data, "title"),
        weight=float(weight),
        speaker=errors.choice(data, "speaker", "manager", ("manager", "client")),
        phrases=phrases,
        synonyms=parse_synonyms(data.get("synonyms"), errors.fail),
        window=errors.choice(data, "window", "all", ("all", "first", "last")),
        window_size=errors.positive_int(data, "window_size", 3),
        min_matches=errors.positive_int(data, "min_matches", 1),
        rule=errors.choice(data, "rule", "phrases", ("phrases", "open_questions")),
        trigger=errors.phrase_list(data, "trigger"),
        respond_within=errors.positive_int(data, "respond_within", 2),
        description=description.strip(),
    )


def _parse(path: Path) -> Checklist:
    data = read_yaml_file(path)
    if not isinstance(data, dict):
        raise ConfigError(f"Ошибка в {path.name}: ожидаются поля «items» и «synonyms».")
    for key in data:
        if key not in ("items", "synonyms"):
            raise ConfigError(f"Ошибка в {path.name}: неизвестное поле «{key}».")

    def fail_global(field_name: str, problem: str) -> ConfigError:
        return ConfigError(f"Ошибка в {path.name}: общее поле «{field_name}» — {problem}.")

    synonyms = parse_synonyms(data.get("synonyms"), fail_global)
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ConfigError(f"Ошибка в {path.name}: поле «items» — нужен непустой список пунктов.")
    seen: set[str] = set()
    items = tuple(_parse_item(path, n, raw, seen) for n, raw in enumerate(raw_items, start=1))
    return Checklist(items=items, synonyms=synonyms)


def load_checklist(path: Path | None) -> Checklist:
    """None → ./checklist.yaml, если есть, иначе встроенный. Ошибки — ConfigError по-русски."""
    if path is None:
        local = Path.cwd() / CHECKLIST_FILE_NAME
        path = local if local.is_file() else DEFAULTS_DIR / CHECKLIST_FILE_NAME
    return _parse(path)
