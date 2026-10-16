"""Демо-база: размер, даты, закономерности (через stats), аудио-звонки, правки и команда `demo`."""

import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from callscope import stats
from callscope.checklist import load_checklist
from callscope.cli import app
from callscope.config import load_config
from callscope.demo import build_demo
from callscope.storage import Storage

runner = CliRunner()
TODAY = date(2026, 10, 16)


@pytest.fixture(scope="module")
def demo_storage(tmp_path_factory) -> Storage:
    folder = tmp_path_factory.mktemp("demo") / "data"
    build_demo(folder, today=TODAY)
    return Storage(folder)


@pytest.fixture(scope="module")
def calls(demo_storage):
    return [c for c in demo_storage.list_calls() if c.status == "done"]


def average(values: list[float]) -> float:
    return sum(values) / len(values)


def test_demo_size_managers_and_dates(calls):
    assert 140 <= len(calls) <= 160
    assert len({c.manager for c in calls}) == 5
    for call in calls:
        assert re.fullmatch(r"[a-z]+", call.manager)
        assert call.matched_template
        day = stats.call_date(call)
        assert TODAY - timedelta(days=8 * 7) < day <= TODAY
    days = sorted(stats.call_date(c) for c in calls)
    assert (TODAY - days[0]).days >= 49 and (TODAY - days[-1]).days <= 7


def test_demo_dialogues_are_varied_russian_calls(demo_storage, calls):
    full = [demo_storage.get(c.id) for c in calls]
    texts = {" ".join(u.text for u in c.dialogue.utterances) for c in full}
    assert len(texts) >= 130
    sample = next(c for c in full if c.audio_path.suffix == ".wav")
    joined = " ".join(u.text for u in sample.dialogue.utterances)
    assert re.search(r"[а-яё]{4,}", joined)
    assert {u.role for u in sample.dialogue.utterances} == {"manager", "client"}


def test_overview_top_failure_is_need(demo_storage, calls):
    checklist = load_checklist(demo_storage.checklist_path)
    thresholds = load_config(demo_storage.config_path).thresholds
    overview = stats.overview(calls, checklist, thresholds, stats.period_preset("quarter", TODAY))
    assert overview.top_failure is not None
    assert overview.top_failure.item_id == "need"


def test_one_manager_fails_need_in_most_calls(calls):
    shares = {}
    for manager in {c.manager for c in calls}:
        mine = [c for c in calls if c.manager == manager]
        failed = [
            c
            for c in mine
            if any(i.item_id == "need" and i.status == "failed" for i in c.score.items)
        ]
        shares[manager] = len(failed) / len(mine)
    assert max(shares.values()) >= 0.8
    assert sorted(shares.values())[-2] < 0.5  # такой он один


def test_one_manager_improves_over_weeks(calls):
    mine = [c for c in calls if c.manager == "ivanov"]
    first = [c.score.score for c in mine if stats.call_date(c) <= TODAY - timedelta(days=42)]
    last = [c.score.score for c in mine if stats.call_date(c) > TODAY - timedelta(days=14)]
    assert average(last) - average(first) >= 20
    halves = (
        [c.score.score for c in mine if stats.call_date(c) <= TODAY - timedelta(days=28)],
        [c.score.score for c in mine if stats.call_date(c) > TODAY - timedelta(days=28)],
    )
    assert average(halves[1]) - average(halves[0]) >= 10


def test_one_manager_clearly_lags_in_ranking(demo_storage, calls):
    ranking = stats.manager_ranking(calls, load_checklist(demo_storage.checklist_path))
    assert len(ranking) == 5
    assert ranking[-1].avg_score <= ranking[-2].avg_score - 10


def test_five_calls_have_real_audio_and_transcripts(demo_storage, calls):
    real = [demo_storage.get(c.id) for c in calls if c.audio_path.suffix == ".m4a"]
    assert len(real) == 5
    for call in real:
        assert call.audio_path.is_file() and call.audio_path.stat().st_size > 10_000
        assert call.raw is not None and call.raw.duration > 20
    generated = next(c for c in calls if c.audio_path.suffix == ".wav")
    assert generated.audio_path.is_file()


def test_some_calls_have_manual_corrections_with_comments(calls):
    corrected = [c for c in calls if c.corrections]
    assert len(corrected) >= 2
    assert all(corr.comment for c in corrected for corr in c.corrections.values())
    changed = [
        c for c in corrected
        if any(corr.status is not None for corr in c.corrections.values())
    ]
    assert changed
    for call in changed:  # правка меняет действующий балл относительно авто-оценки
        auto = {i.item_id: i.status for i in call.auto_items}
        assert any(
            corr.status is not None and corr.status != auto[item_id]
            for item_id, corr in call.corrections.items()
        )


def test_missing_examples_gives_only_generated_calls(tmp_path):
    summary = build_demo(tmp_path / "d", today=TODAY, examples_dir=tmp_path / "nothing")
    assert summary.audio_calls == 0
    assert 140 <= summary.calls <= 160


def test_demo_command_creates_base_and_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "my-demo"
    first = runner.invoke(app, ["demo", "--data-dir", str(target)])
    assert first.exit_code == 0, first.output
    assert (target / "callscope.db").is_file()
    assert not (tmp_path / "callscope-data").exists()

    second = runner.invoke(app, ["demo", "--data-dir", str(target)])
    assert second.exit_code == 2
    assert "--force" in second.output

    count = len(Storage(target).list_calls())
    third = runner.invoke(app, ["demo", "--data-dir", str(target), "--force"])
    assert third.exit_code == 0, third.output
    assert len(Storage(target).list_calls()) == count


def test_demo_force_never_wipes_working_data_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CALLSCOPE_DATA_DIR", raising=False)
    work = Storage(Path("callscope-data"))
    work.add_upload("ivanov_2026-10-12_1530.mp3", b"abc")
    result = runner.invoke(app, ["demo", "--data-dir", "callscope-data", "--force"])
    assert result.exit_code == 2
    assert len(work.list_calls()) == 1
