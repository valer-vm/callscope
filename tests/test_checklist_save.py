"""Запись чек-листа: dump/save/parse_checklist_text."""

import pytest

from callscope.checklist import (
    Checklist,
    ChecklistItem,
    dump_checklist,
    load_checklist,
    parse_checklist_text,
    save_checklist,
)
from callscope.errors import ConfigError

FULL_ITEM = ChecklistItem(
    id="objection",
    title="Отработка возражения",
    weight=12.5,
    speaker="client",
    phrases=("скидка", "гарантия"),
    statement_phrases=("нужен счёт",),
    synonyms={"скидка": ("скидочка", "акция")},
    window="last",
    window_size=4,
    min_matches=2,
    rule="open_questions",
    trigger=("дорого",),
    respond_within=3,
    description="Менеджер отвечает на возражение.\nВторая строка.",
)


def test_dump_roundtrips_builtin_checklist():
    builtin = load_checklist(None)
    assert parse_checklist_text(dump_checklist(builtin)) == builtin


def test_dump_roundtrips_item_with_all_fields():
    checklist = Checklist(items=(FULL_ITEM,), synonyms={"цена": ("стоимость",)})
    assert parse_checklist_text(dump_checklist(checklist)) == checklist


def test_dump_skips_defaults():
    item = ChecklistItem(id="greeting", title="Приветствие", weight=5, phrases=("здравствуйте",))
    text = dump_checklist(Checklist(items=(item,)))
    data = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "window" not in data and "respond_within" not in data and "speaker" not in data
    assert "Приветствие" in text  # кириллица не экранируется


def test_saved_files_keep_reference_comments(tmp_path):
    """Сохранение из панели не стирает пояснения к полям (они из встроенных файлов)."""
    from callscope.config import load_config, save_config

    checklist_path = tmp_path / "checklist.yaml"
    save_checklist(load_checklist(None), checklist_path)
    text = checklist_path.read_text(encoding="utf-8")
    assert "# Поля пункта:" in text and "# Общие синонимы для всех пунктов." in text
    assert load_checklist(checklist_path) == load_checklist(None)

    config_path = tmp_path / "callscope.yaml"
    save_config(load_config(None), config_path)
    text = config_path.read_text(encoding="utf-8")
    assert "# Шаблон имени файла записи." in text
    assert load_config(config_path) == load_config(None)


def test_save_writes_file_loadable_back(tmp_path):
    path = tmp_path / "checklist.yaml"
    checklist = Checklist(items=(FULL_ITEM,))
    save_checklist(checklist, path)
    assert load_checklist(path) == checklist
    assert not list(tmp_path.glob("*.tmp"))


def test_save_invalid_keeps_old_file(tmp_path):
    path = tmp_path / "checklist.yaml"
    good = Checklist(items=(FULL_ITEM,))
    save_checklist(good, path)
    before = path.read_bytes()
    broken = Checklist(items=(ChecklistItem(id="Bad Id", title="x", weight=1, phrases=("a",)),))
    with pytest.raises(ConfigError):
        save_checklist(broken, path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_parse_text_validates_without_file():
    text = "items:\n  - id: a\n    title: Пункт\n    weight: 0\n    phrases: [x]\n"
    with pytest.raises(ConfigError) as info:
        parse_checklist_text(text, "чек-лист")
    assert "weight" in info.value.message_ru and "чек-лист" in info.value.message_ru


def test_parse_text_rejects_broken_yaml():
    with pytest.raises(ConfigError):
        parse_checklist_text("items: [", "чек-лист")
