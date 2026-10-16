from pathlib import Path

import pytest

from callscope.checklist import Checklist, load_checklist
from callscope.errors import ConfigError

ROOT = Path(__file__).resolve().parents[1]

VALID = """
synonyms:
  цена: [стоимость]
items:
  - id: greeting
    title: Приветствие
    weight: 10
    window: first
    window_size: 2
    phrases: [здравствуйте]
    synonyms:
      здравствуйте: [добрый день]
    description: Менеджер поздоровался.
  - id: objection
    title: Возражение
    weight: 20
    phrases: [скидка]
    trigger: [дорого]
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "checklist.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_file_parsed_with_defaults(tmp_path):
    checklist = load_checklist(write(tmp_path, VALID))
    assert isinstance(checklist, Checklist)
    assert [item.id for item in checklist.items] == ["greeting", "objection"]
    greeting, objection = checklist.items
    assert greeting.weight == 10
    assert greeting.window == "first" and greeting.window_size == 2
    assert greeting.synonyms == {"здравствуйте": ("добрый день",)}
    assert greeting.speaker == "manager"
    assert objection.window == "all" and objection.window_size == 3
    assert objection.min_matches == 1 and objection.respond_within == 2
    assert objection.rule == "phrases"
    assert objection.trigger == ("дорого",)
    assert checklist.synonyms == {"цена": ("стоимость",)}


@pytest.mark.parametrize(
    ("bad_item", "field"),
    [
        ("  - id: farewell\n    title: Прощание\n    phrases: [пока]\n", "weight"),
        ("  - id: farewell\n    title: Прощание\n    weight: 0\n    phrases: [пока]\n", "weight"),
        ("  - id: farewell\n    title: Прощание\n    weight: -3\n    phrases: [пока]\n", "weight"),
        (
            "  - id: farewell\n    title: Прощание\n    weight: много\n    phrases: [пока]\n",
            "weight",
        ),
        ("  - id: greeting\n    title: Ещё раз\n    weight: 5\n    phrases: [пока]\n", "id"),
        (
            "  - id: farewell\n    title: Прощание\n    weight: 5\n    phrases: [пока]\n"
            "    windw: last\n",
            "windw",
        ),
        (
            "  - id: farewell\n    title: Прощание\n    weight: 5\n    phrases: [пока]\n"
            "    rule: magic\n",
            "rule",
        ),
        (
            "  - id: farewell\n    title: Прощание\n    weight: 5\n    phrases: [пока]\n"
            "    window: middle\n",
            "window",
        ),
        ("  - id: farewell\n    title: Прощание\n    weight: 5\n", "phrases"),
        ("  - id: прощание\n    title: Прощание\n    weight: 5\n    phrases: [пока]\n", "id"),
    ],
)
def test_errors_name_item_and_field(tmp_path, bad_item, field):
    with pytest.raises(ConfigError) as exc:
        load_checklist(write(tmp_path, VALID + bad_item))
    message = exc.value.message_ru
    assert f"«{field}»" in message
    assert "пункт" in message
    assert "farewell" in message or "greeting" in message or "прощание" in message.lower()


def test_broken_yaml_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_checklist(write(tmp_path, "items: [\n"))


def test_empty_items_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_checklist(write(tmp_path, "items: []\n"))


def test_none_without_local_file_gives_default_six_items(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    checklist = load_checklist(None)
    ids = [item.id for item in checklist.items]
    assert ids == ["greeting", "need", "solution", "objection", "next_step", "farewell"]
    by_id = {item.id: item for item in checklist.items}
    assert by_id["need"].rule == "open_questions"
    assert by_id["objection"].trigger
    for item in checklist.items:
        assert item.weight > 0 and item.phrases and item.description
    assert any(item.synonyms for item in checklist.items) or checklist.synonyms


def test_none_prefers_local_file(tmp_path, monkeypatch):
    write(tmp_path, VALID)
    monkeypatch.chdir(tmp_path)
    assert [item.id for item in load_checklist(None).items] == ["greeting", "objection"]


def test_root_copy_matches_packaged_default():
    packaged = ROOT / "src" / "callscope" / "defaults" / "checklist.yaml"
    assert (ROOT / "checklist.yaml").read_text(encoding="utf-8") == packaged.read_text(
        encoding="utf-8"
    )


def test_every_default_item_has_synonyms():
    for path in (
        ROOT / "checklist.yaml",
        ROOT / "src" / "callscope" / "defaults" / "checklist.yaml",
    ):
        checklist = load_checklist(path)
        for item in checklist.items:
            assert item.synonyms, f"у пункта {item.id} пустые synonyms в {path}"
