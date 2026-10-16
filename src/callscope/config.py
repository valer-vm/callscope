"""Загрузка callscope.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from callscope.errors import ConfigError
from callscope.model import Channel

DEFAULTS_DIR = Path(__file__).parent / "defaults"
CONFIG_FILE_NAME = "callscope.yaml"


@dataclass(frozen=True)
class Thresholds:
    good: float = 80
    medium: float = 50


@dataclass(frozen=True)
class SemanticConfig:
    enabled: bool = False
    url: str = "http://localhost:11434"
    model: str = "qwen2.5:7b"
    timeout: float = 120


@dataclass(frozen=True)
class AppConfig:
    filename_template: str
    date_format: str
    time_format: str
    model_size: str
    model_path: str | None
    manager_channel: Literal["auto", "left", "right"]
    manager_channel_overrides: dict[str, Channel]
    intro_phrases: tuple[str, ...]
    company_names: tuple[str, ...]
    mono_turn_gap: float
    thresholds: Thresholds = field(default_factory=Thresholds)
    semantic: SemanticConfig = field(default_factory=SemanticConfig)


def read_yaml_file(path: Path) -> Any:
    """Читает YAML; любые проблемы чтения — ConfigError по-русски."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"Файл настроек {path} не найден.") from None
    except OSError as exc:
        raise ConfigError(f"Не удалось прочитать файл {path}: {exc.strerror}.") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (строка {mark.line + 1})" if mark is not None else ""
        raise ConfigError(f"Файл {path} — ошибка синтаксиса YAML{where}.") from None


def _fail(path: Path, field_name: str, problem: str) -> ConfigError:
    return ConfigError(f"Ошибка в {path.name}: поле «{field_name}» — {problem}.")


def _str(path: Path, name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(path, name, "нужна непустая строка")
    return value


def _number(path: Path, name: str, value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _fail(path, name, "нужно число")
    if positive and value <= 0:
        raise _fail(path, name, "нужно число больше нуля")
    return float(value)


def _bool(path: Path, name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise _fail(path, name, "нужно true или false")
    return value


def _str_list(path: Path, name: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise _fail(path, name, "нужен список строк")
    return tuple(value)


def _mapping(path: Path, name: str, value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _fail(path, name, "нужен набор «ключ: значение»")
    return value


def _check_keys(path: Path, prefix: str, data: dict[str, Any], allowed: set[str]) -> None:
    for key in data:
        if key not in allowed:
            raise _fail(path, f"{prefix}{key}", "неизвестное поле")


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if (
            isinstance(value, dict)
            and isinstance(merged.get(key), dict)
            and key
            in (
                "thresholds",
                "semantic",
            )
        ):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


_TOP_KEYS = {
    "filename_template",
    "date_format",
    "time_format",
    "model_size",
    "model_path",
    "manager_channel",
    "manager_channel_overrides",
    "intro_phrases",
    "company_names",
    "mono_turn_gap",
    "thresholds",
    "semantic",
}


def _build(path: Path, data: dict[str, Any]) -> AppConfig:
    _check_keys(path, "", data, _TOP_KEYS)

    manager_channel = data.get("manager_channel")
    if manager_channel not in ("auto", "left", "right"):
        raise _fail(path, "manager_channel", "допустимо auto, left или right")

    overrides_raw = _mapping(
        path, "manager_channel_overrides", data.get("manager_channel_overrides")
    )
    overrides: dict[str, Channel] = {}
    for file_name, side in overrides_raw.items():
        if side not in ("left", "right"):
            raise _fail(path, f"manager_channel_overrides.{file_name}", "допустимо left или right")
        overrides[str(file_name)] = side

    model_path = data.get("model_path")
    if model_path is not None:
        model_path = _str(path, "model_path", model_path)

    thresholds_raw = _mapping(path, "thresholds", data.get("thresholds"))
    _check_keys(path, "thresholds.", thresholds_raw, {"good", "medium"})
    thresholds = Thresholds(
        good=_number(path, "thresholds.good", thresholds_raw.get("good", 80)),
        medium=_number(path, "thresholds.medium", thresholds_raw.get("medium", 50)),
    )
    if not 0 <= thresholds.medium <= thresholds.good <= 100:
        raise _fail(path, "thresholds", "нужно 0 ≤ medium ≤ good ≤ 100")

    semantic_raw = _mapping(path, "semantic", data.get("semantic"))
    _check_keys(path, "semantic.", semantic_raw, {"enabled", "url", "model", "timeout"})
    defaults = SemanticConfig()
    semantic = SemanticConfig(
        enabled=_bool(path, "semantic.enabled", semantic_raw.get("enabled", defaults.enabled)),
        url=_str(path, "semantic.url", semantic_raw.get("url", defaults.url)),
        model=_str(path, "semantic.model", semantic_raw.get("model", defaults.model)),
        timeout=_number(
            path, "semantic.timeout", semantic_raw.get("timeout", defaults.timeout), positive=True
        ),
    )

    return AppConfig(
        filename_template=_str(path, "filename_template", data.get("filename_template")),
        date_format=_str(path, "date_format", data.get("date_format")),
        time_format=_str(path, "time_format", data.get("time_format")),
        model_size=_str(path, "model_size", data.get("model_size")),
        model_path=model_path,
        manager_channel=manager_channel,
        manager_channel_overrides=overrides,
        intro_phrases=_str_list(path, "intro_phrases", data.get("intro_phrases")),
        company_names=_str_list(path, "company_names", data.get("company_names")),
        mono_turn_gap=_number(path, "mono_turn_gap", data.get("mono_turn_gap"), positive=True),
        thresholds=thresholds,
        semantic=semantic,
    )


def _read_mapping(path: Path) -> dict[str, Any]:
    data = read_yaml_file(path)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"Ошибка в {path.name}: ожидается набор полей «ключ: значение».")
    return data


def load_config(path: Path | None) -> AppConfig:
    """None → ./callscope.yaml, если есть, иначе встроенный default. Ошибки — ConfigError."""
    default_path = DEFAULTS_DIR / CONFIG_FILE_NAME
    base = _read_mapping(default_path)
    if path is None:
        local = Path.cwd() / CONFIG_FILE_NAME
        if not local.is_file():
            return _build(default_path, base)
        path = local
    return _build(path, _merge(base, _read_mapping(path)))
