"""Загрузка и проверка checklist.yaml."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

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
    "statement_phrases",
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
    statement_phrases: tuple[str, ...] = ()
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
    statement_phrases = errors.phrase_list(data, "statement_phrases")
    if not phrases and not statement_phrases:
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
        statement_phrases=statement_phrases,
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
    return _parse_data(path, read_yaml_file(path))


def _parse_data(path: Path, data: Any) -> Checklist:
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


def parse_checklist_text(text: str, source: str = "чек-лист") -> Checklist:
    """Проверяет текст чек-листа без файла; `source` попадает в сообщения об ошибках."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ConfigError(f"Ошибка в {source}: синтаксис YAML нарушен.") from None
    return _parse_data(Path(source), data)


def _number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def _item_to_dict(item: ChecklistItem) -> dict[str, Any]:
    """Поля пункта в порядке чек-листа; значения по умолчанию не пишутся."""
    data: dict[str, Any] = {"id": item.id, "title": item.title, "weight": _number(item.weight)}
    if item.speaker != "manager":
        data["speaker"] = item.speaker
    if item.rule != "phrases":
        data["rule"] = item.rule
    if item.window != "all":
        data["window"] = item.window
    if item.window_size != 3:
        data["window_size"] = item.window_size
    if item.min_matches != 1:
        data["min_matches"] = item.min_matches
    if item.trigger:
        data["trigger"] = list(item.trigger)
    if item.respond_within != 2:
        data["respond_within"] = item.respond_within
    if item.phrases:
        data["phrases"] = list(item.phrases)
    if item.statement_phrases:
        data["statement_phrases"] = list(item.statement_phrases)
    if item.synonyms:
        data["synonyms"] = {word: list(repl) for word, repl in item.synonyms.items()}
    if item.description:
        data["description"] = item.description
    return data


def dump_checklist(checklist: Checklist) -> str:
    """YAML того же формата, что `checklist.yaml`; `load_checklist` читает его обратно."""
    data: dict[str, Any] = {}
    if checklist.synonyms:
        data["synonyms"] = {word: list(repl) for word, repl in checklist.synonyms.items()}
    data["items"] = [_item_to_dict(item) for item in checklist.items]
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=88)


def save_checklist(checklist: Checklist, path: Path) -> None:
    """Атомарная запись. Файл не меняется, если результат не проходит `load_checklist`."""
    text = dump_checklist(checklist)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        load_checklist(tmp)
        os.replace(tmp, path)
    except OSError as exc:
        raise ConfigError(f"Не удалось сохранить чек-лист: {exc.strerror}.") from None
    finally:
        tmp.unlink(missing_ok=True)
