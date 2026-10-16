"""Запись callscope.yaml: save_config."""

from dataclasses import replace

import pytest

from callscope.config import SemanticConfig, Thresholds, load_config, save_config
from callscope.errors import ConfigError


def test_roundtrip_builtin(tmp_path):
    config = load_config(None)
    path = tmp_path / "callscope.yaml"
    save_config(config, path)
    assert load_config(path) == config
    assert not list(tmp_path.glob("*.tmp"))


def test_roundtrip_changed_fields(tmp_path):
    config = replace(
        load_config(None),
        filename_template="{any}_{manager}_{date}",
        date_format="%d.%m.%Y",
        model_path="/models/whisper",
        manager_channel_overrides={"a.mp3": "left"},
        thresholds=Thresholds(good=70, medium=40),
        semantic=SemanticConfig(enabled=True, timeout=30),
    )
    path = tmp_path / "callscope.yaml"
    save_config(config, path)
    assert load_config(path) == config


def test_invalid_config_keeps_old_file(tmp_path):
    path = tmp_path / "callscope.yaml"
    save_config(load_config(None), path)
    before = path.read_bytes()
    bad = replace(load_config(None), filename_template="")
    with pytest.raises(ConfigError):
        save_config(bad, path)
    assert path.read_bytes() == before
