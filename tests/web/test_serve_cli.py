"""Настройки панели из `.env`, команды `callscope serve` и `callscope import`."""

from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from callscope import cli
from callscope.cache import TranscriptCache
from callscope.config import load_config
from callscope.errors import ConfigError
from callscope.model import RawTranscript
from callscope.storage import Storage
from callscope.web.settings import load_web_settings

ENV_NAMES = ("CALLSCOPE_PASSWORD", "CALLSCOPE_SECRET_KEY", "CALLSCOPE_DATA_DIR",
             "CALLSCOPE_HOST", "CALLSCOPE_PORT")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    yield
    for name in ENV_NAMES:  # load_dotenv пишет в os.environ мимо monkeypatch
        os.environ.pop(name, None)


def test_no_password_is_config_error(tmp_path):
    with pytest.raises(ConfigError) as error:
        load_web_settings(tmp_path / "data", demo=False)
    assert "CALLSCOPE_PASSWORD" in error.value.message_ru
    assert ".env" in error.value.message_ru


def test_env_file_from_cwd_and_generated_secret(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "CALLSCOPE_PASSWORD=from-env-file\nCALLSCOPE_DATA_DIR=panel-data\n", encoding="utf-8"
    )
    first = load_web_settings(None, demo=False)
    assert first.password == "from-env-file"
    assert first.data_dir.name == "panel-data"
    assert len(first.secret_key) >= 32
    assert "from-env-file" not in repr(first) and first.secret_key not in repr(first)
    # ключ сохраняется в папке данных: перезапуск не разлогинивает
    monkeypatch.delenv("CALLSCOPE_PASSWORD")
    assert load_web_settings(None, demo=False).secret_key == first.secret_key


def test_secret_key_from_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLSCOPE_PASSWORD", "p")
    monkeypatch.setenv("CALLSCOPE_SECRET_KEY", "explicit-test-key")
    settings = load_web_settings(tmp_path / "d", demo=False)
    assert settings.secret_key == "explicit-test-key"
    assert not (tmp_path / "d" / ".secret_key").exists()


def test_demo_uses_demo_folder(monkeypatch):
    monkeypatch.setenv("CALLSCOPE_PASSWORD", "p")
    monkeypatch.setenv("CALLSCOPE_DATA_DIR", "work-data")
    settings = load_web_settings(None, demo=True)
    assert settings.demo and settings.data_dir.name == "callscope-data-demo"


def test_serve_without_password_exits_2(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(cli, "_run_server", lambda *a, **k: started.append(a))
    result = CliRunner().invoke(cli.app, ["serve", "--data-dir", str(tmp_path / "d")])
    assert result.exit_code == 2
    assert "CALLSCOPE_PASSWORD" in result.output
    assert started == []


def test_serve_prints_address_and_runs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("CALLSCOPE_PASSWORD", "test-only")
    monkeypatch.setattr(cli, "_run_server", lambda app, host, port: calls.append((host, port)))
    result = CliRunner().invoke(
        cli.app, ["serve", "--data-dir", str(tmp_path / "d"), "--port", "8123"]
    )
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:8123" in result.output
    assert calls == [("127.0.0.1", 8123)]
    assert "test-only" not in result.output


def test_serve_demo_without_demo_base_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLSCOPE_PASSWORD", "test-only")
    monkeypatch.setattr(cli, "_run_server", lambda *a, **k: None)
    result = CliRunner().invoke(cli.app, ["serve", "--demo"])
    assert result.exit_code == 2
    assert "callscope demo" in result.output


def _recordings(folder):
    folder.mkdir()
    (folder / "ivanov_2026-10-12_1530.m4a").write_bytes(b"good.json")
    (folder / "petrova_2026-10-13_1015.mp3").write_bytes(b"no_need.json")
    (folder / "notes.txt").write_text("не запись", encoding="utf-8")
    return folder


def test_import_queues_and_skips_known(tmp_path):
    folder = _recordings(tmp_path / "calls")
    data = tmp_path / "data"
    runner = CliRunner()
    first = runner.invoke(cli.app, ["import", str(folder), "--data-dir", str(data)])
    assert first.exit_code == 0, first.output
    assert "поставлено 2, уже были 0" in first.output
    second = runner.invoke(cli.app, ["import", str(folder), "--data-dir", str(data)])
    assert "поставлено 0, уже были 2" in second.output
    records = Storage(data).list_calls()
    assert sorted(r.file_name for r in records) == [
        "ivanov_2026-10-12_1530.m4a",
        "petrova_2026-10-13_1015.mp3",
    ]
    assert all(r.status == "queued" for r in records)


def test_import_reuses_stage1_cache(tmp_path, raw_from_fixture):
    folder = _recordings(tmp_path / "calls")
    data = tmp_path / "data"
    storage = Storage(data)
    config = load_config(storage.config_path)
    audio = folder / "ivanov_2026-10-12_1530.m4a"
    stage1 = TranscriptCache(folder / ".callscope-cache")
    key = stage1.key(audio, config.model_path or config.model_size, config.mono_turn_gap)
    raw = raw_from_fixture("good.json")
    stage1.put(key, raw)

    result = CliRunner().invoke(cli.app, ["import", str(folder), "--data-dir", str(data)])
    assert result.exit_code == 0, result.output
    assert "из кэша: 1" in result.output
    copied = data / "cache" / f"{key}.json"
    assert RawTranscript.from_json(json.loads(copied.read_text(encoding="utf-8"))) == raw


def test_import_missing_folder_exits_2(tmp_path):
    result = CliRunner().invoke(cli.app, ["import", str(tmp_path / "nope"),
                                          "--data-dir", str(tmp_path / "d")])
    assert result.exit_code == 2


def test_import_empty_folder_exits_1(tmp_path):
    (tmp_path / "empty").mkdir()
    result = CliRunner().invoke(cli.app, ["import", str(tmp_path / "empty"),
                                          "--data-dir", str(tmp_path / "d")])
    assert result.exit_code == 1


def test_server_does_not_trust_forwarded_headers(monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))
    cli._run_server(object(), "127.0.0.1", 8000)
    # иначе uvicorn подставляет X-Forwarded-For от 127.0.0.1 как адрес клиента
    assert seen["proxy_headers"] is False
