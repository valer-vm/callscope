"""CLI analyze: коды выхода и сборка отчёта. Transcriber подменён — модель и аудио не нужны.

Подставная «запись» — текстовый файл с именем JSON-фикстуры диалога; содержимое
`BROKEN` изображает нечитаемый файл.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from callscope import cli, pipeline
from callscope.errors import AudioError
from callscope.model import RawTranscript

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


class FakeTranscriber:
    calls: list[str] = []
    created: list[tuple[str, str | None]] = []

    def __init__(self, model_size: str, model_path: str | None = None) -> None:
        FakeTranscriber.created.append((model_size, model_path))

    def transcribe(self, path: Path, mono_turn_gap: float, on_progress=None) -> RawTranscript:
        FakeTranscriber.calls.append(path.name)
        content = path.read_text(encoding="utf-8").strip()
        if content == "BROKEN":
            raise AudioError(f"Не удалось прочитать запись {path.name}: файл повреждён.")
        data = json.loads((FIXTURES / content).read_text(encoding="utf-8"))
        # Менеджер в фикстурах слева; канал задаём по роли.
        segments = [
            {
                "start": u["start"],
                "end": u["end"],
                "text": u["text"],
                "channel": "left" if u["role"] == "manager" else "right",
            }
            for u in data["utterances"]
        ]
        if on_progress:
            on_progress(0.5)
        duration = segments[-1]["end"] if segments else 0.0
        return RawTranscript.from_json(
            {"mode": "stereo", "duration": duration, "segments": segments}
        )


@pytest.fixture(autouse=True)
def fake_transcriber(monkeypatch, tmp_path):
    FakeTranscriber.calls = []
    FakeTranscriber.created = []
    monkeypatch.setattr(cli, "Transcriber", FakeTranscriber)
    # Без локальных callscope.yaml / checklist.yaml — встроенные настройки.
    monkeypatch.chdir(tmp_path)
    return FakeTranscriber


def make_calls(folder: Path, files: dict[str, str]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (folder / name).write_text(content, encoding="utf-8")
    return folder


GOOD_SET = {
    "ivanov_2026-10-12_1530.m4a": "good.json",
    "petrova_2026-10-13_1015.mp3": "no_need.json",
    "sidorov_2026-10-14_1400.WAV": "failed_objection.json",
}


def run(*args: str):
    return runner.invoke(cli.app, ["analyze", *args])


def report_dirs(out: Path) -> list[Path]:
    return sorted(p for p in out.iterdir() if p.is_dir())


def test_analyze_builds_both_reports_and_prints_path(tmp_path):
    calls = make_calls(tmp_path / "calls", {**GOOD_SET, "notes.txt": "good.json"})
    out = tmp_path / "out"
    result = run(str(calls), "--out", str(out))
    assert result.exit_code == 0, result.output
    [folder] = report_dirs(out)
    assert (folder / "report.html").is_file()
    assert (folder / "report.xlsx").is_file()
    assert str(folder) in result.output.replace("\n", "")
    html = (folder / "report.html").read_text(encoding="utf-8")
    for manager in ("ivanov", "petrova", "sidorov"):
        assert manager in html
    # .txt не запись — не расшифровывается.
    assert sorted(FakeTranscriber.calls) == sorted(GOOD_SET)


def test_default_out_is_reports_in_cwd_and_second_run_gets_suffix(tmp_path):
    calls = make_calls(tmp_path / "calls", GOOD_SET)
    assert run(str(calls)).exit_code == 0
    assert run(str(calls)).exit_code == 0
    folders = report_dirs(tmp_path / "reports")
    assert len(folders) >= 2
    for folder in folders:
        assert (folder / "report.html").is_file()


def test_second_run_uses_cache_and_no_cache_transcribes_again(tmp_path):
    calls = make_calls(tmp_path / "calls", GOOD_SET)
    out = tmp_path / "out"
    assert run(str(calls), "--out", str(out)).exit_code == 0
    assert len(FakeTranscriber.calls) == 3
    assert (calls / ".callscope-cache").is_dir()

    second = run(str(calls), "--out", str(out))
    assert second.exit_code == 0
    assert len(FakeTranscriber.calls) == 3
    assert "из кэша" in second.output

    assert run(str(calls), "--out", str(out), "--no-cache").exit_code == 0
    assert len(FakeTranscriber.calls) == 6


def test_empty_folder_exit_1_without_report(tmp_path):
    calls = make_calls(tmp_path / "calls", {"readme.txt": "x"})
    out = tmp_path / "out"
    result = run(str(calls), "--out", str(out))
    assert result.exit_code == 1
    assert "нет записей (mp3, wav, ogg, m4a)" in result.output
    assert not out.exists()


def test_missing_folder_exit_2(tmp_path):
    result = run(str(tmp_path / "nope"))
    assert result.exit_code == 2
    assert "не найдена" in result.output
    assert "Traceback" not in result.output


def test_bad_checklist_exit_2_before_transcription(tmp_path):
    calls = make_calls(tmp_path / "calls", GOOD_SET)
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "items:\n  - id: greeting\n    title: Привет\n    weight: -1\n", encoding="utf-8"
    )
    result = run(str(calls), "--checklist", str(bad))
    assert result.exit_code == 2
    assert "weight" in result.output
    assert FakeTranscriber.calls == []


def test_bad_config_exit_2(tmp_path):
    calls = make_calls(tmp_path / "calls", GOOD_SET)
    bad = tmp_path / "cfg.yaml"
    bad.write_text("manager_channel: middle\n", encoding="utf-8")
    result = run(str(calls), "--config", str(bad))
    assert result.exit_code == 2
    assert "manager_channel" in result.output


def test_remote_ollama_url_exit_2_before_transcription(tmp_path):
    calls = make_calls(tmp_path / "calls", GOOD_SET)
    bad = tmp_path / "cfg.yaml"
    bad.write_text('semantic: {url: "http://ollama.example.com:11434"}\n', encoding="utf-8")
    result = run(str(calls), "--config", str(bad))
    assert result.exit_code == 2
    assert "semantic.url" in result.output
    assert FakeTranscriber.calls == []


def test_broken_file_is_skipped_and_listed(tmp_path):
    calls = make_calls(tmp_path / "calls", {**GOOD_SET, "broken_2026-10-14_1000.ogg": "BROKEN"})
    out = tmp_path / "out"
    result = run(str(calls), "--out", str(out))
    assert result.exit_code == 0, result.output
    [folder] = report_dirs(out)
    html = (folder / "report.html").read_text(encoding="utf-8")
    assert "broken_2026-10-14_1000.ogg" in html
    assert "повреждён" in html


def test_all_files_broken_exit_1_without_report(tmp_path):
    calls = make_calls(tmp_path / "calls", {"a.mp3": "BROKEN", "b.wav": "BROKEN"})
    out = tmp_path / "out"
    result = run(str(calls), "--out", str(out))
    assert result.exit_code == 1
    assert "a.mp3" in result.output and "b.wav" in result.output
    assert not out.exists()


def test_manager_channel_flag_overrides_auto(tmp_path, monkeypatch):
    calls = make_calls(tmp_path / "calls", {"ivanov_2026-10-12_1530.m4a": "good.json"})
    captured = []
    real_write_html = cli.write_html

    def spy(summary, path):
        captured.append(summary)
        real_write_html(summary, path)

    monkeypatch.setattr(cli, "write_html", spy)
    out = tmp_path / "out"
    assert run(str(calls), "--out", str(out)).exit_code == 0
    assert run(str(calls), "--out", str(out), "--manager-channel", "right").exit_code == 0
    auto, manual = (s.calls[0] for s in captured)
    assert (auto.dialogue.manager_channel, auto.dialogue.role_source) == ("left", "intro")
    assert (manual.dialogue.manager_channel, manual.dialogue.role_source) == ("right", "manual")
    assert manual.score.score < auto.score.score


def test_manager_channel_flag_rejects_unknown_value(tmp_path):
    calls = make_calls(tmp_path / "calls", GOOD_SET)
    assert run(str(calls), "--manager-channel", "middle").exit_code == 2


def test_model_flag_reaches_transcriber(tmp_path):
    calls = make_calls(tmp_path / "calls", {"ivanov_2026-10-12_1530.m4a": "good.json"})
    assert run(str(calls), "--model", "tiny", "--out", str(tmp_path / "o")).exit_code == 0
    assert FakeTranscriber.created[0][0] == "tiny"


def test_config_and_checklist_flags_are_used(tmp_path):
    calls = make_calls(tmp_path / "calls", {"call-ivanov.m4a": "good.json"})
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text('filename_template: "call-{manager}"\n', encoding="utf-8")
    checklist = tmp_path / "list.yaml"
    checklist.write_text(
        "items:\n"
        "  - id: hello\n"
        "    title: Сказал «здравствуйте»\n"
        "    weight: 1\n"
        "    phrases: [здравствуйте]\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    result = run(str(calls), "--config", str(cfg), "--checklist", str(checklist), "--out", str(out))
    assert result.exit_code == 0, result.output
    [folder] = report_dirs(out)
    html = (folder / "report.html").read_text(encoding="utf-8")
    assert "Сказал «здравствуйте»" in html
    assert "ivanov" in html


def test_semantic_flag_calls_recheck(tmp_path, monkeypatch):
    calls = make_calls(tmp_path / "calls", {"ivanov_2026-10-13_1015.m4a": "no_need.json"})
    seen: list[str] = []

    def fake_recheck(dialogue, checklist, score, config, thresholds):
        seen.append("called")
        return score, ["Ollama недоступна — подставная проверка."]

    monkeypatch.setattr(pipeline, "recheck", fake_recheck)
    out = tmp_path / "out"
    assert run(str(calls), "--out", str(out), "--no-semantic").exit_code == 0
    assert seen == []
    assert run(str(calls), "--out", str(out), "--semantic").exit_code == 0
    assert seen == ["called"]


def test_unmatched_filename_warns(tmp_path):
    calls = make_calls(tmp_path / "calls", {"zapis1.m4a": "good.json"})
    out = tmp_path / "out"
    result = run(str(calls), "--out", str(out))
    assert result.exit_code == 0
    assert "Не определён" in result.output


def test_out_without_write_permission_exit_2_before_transcription(tmp_path):
    calls = make_calls(tmp_path / "calls", GOOD_SET)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        result = run(str(calls), "--out", str(locked / "reports"))
    finally:
        locked.chmod(0o700)
    assert result.exit_code == 2, result.output
    assert "Нет прав на запись" in result.output
    assert "Traceback" not in result.output
    assert FakeTranscriber.calls == []


def test_report_write_error_exit_2_without_traceback(tmp_path, monkeypatch):
    calls = make_calls(tmp_path / "calls", GOOD_SET)

    def denied(summary, path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(cli, "write_html", denied)
    result = run(str(calls), "--out", str(tmp_path / "out"))
    assert result.exit_code == 2, result.output
    assert "Не удалось записать отчёт" in result.output
    assert "Traceback" not in result.output
