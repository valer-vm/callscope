from pathlib import Path

import pytest

from callscope.config import AppConfig, Thresholds, load_config
from callscope.errors import ConfigError


def test_none_without_local_file_uses_builtin_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(None)
    assert isinstance(config, AppConfig)
    assert config.filename_template == "{manager}_{date}_{time}"
    assert config.date_format == "%Y-%m-%d"
    assert config.time_format == "%H%M"
    assert config.model_size == "small"
    assert config.model_path is None
    assert config.manager_channel == "auto"
    assert config.mono_turn_gap == 0.8
    assert config.thresholds == Thresholds(good=80, medium=50)
    assert config.semantic.enabled is False
    assert config.semantic.url == "http://localhost:11434"
    assert "СтройДом" in config.company_names
    assert "меня зовут" in config.intro_phrases


def test_none_picks_file_in_current_directory(tmp_path, monkeypatch):
    (tmp_path / "callscope.yaml").write_text("model_size: medium\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    config = load_config(None)
    assert config.model_size == "medium"
    assert config.filename_template == "{manager}_{date}_{time}"


def test_explicit_file_overrides_nested_fields(tmp_path):
    path = tmp_path / "my.yaml"
    path.write_text(
        "filename_template: '{date}_{manager}'\n"
        "thresholds:\n  good: 90\n"
        "manager_channel_overrides:\n  a.mp3: right\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.filename_template == "{date}_{manager}"
    assert config.thresholds == Thresholds(good=90, medium=50)
    assert config.manager_channel_overrides == {"a.mp3": "right"}


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ("mono_turn_gap: быстро\n", "mono_turn_gap"),
        ("thresholds:\n  good: много\n", "thresholds.good"),
        ("manager_channel: middle\n", "manager_channel"),
        ("intro_phrases: меня зовут\n", "intro_phrases"),
        ("semantic:\n  enabled: 3\n", "semantic.enabled"),
        ("unknown_key: 1\n", "unknown_key"),
        ("thresholds:\n  good: 40\n  medium: 60\n", "thresholds"),
    ],
)
def test_wrong_field_raises_config_error_naming_field(tmp_path, text, field):
    path = tmp_path / "bad.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert field in exc.value.message_ru


def test_missing_explicit_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_broken_yaml_is_config_error(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("a: [1, 2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_root_copy_matches_packaged_default():
    root = Path(__file__).resolve().parents[1]
    packaged = root / "src" / "callscope" / "defaults" / "callscope.yaml"
    assert (root / "callscope.yaml").read_text(encoding="utf-8") == packaged.read_text(
        encoding="utf-8"
    )
