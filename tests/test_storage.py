"""Хранилище: приём записей, очередь, правки, удаление. SQLite и файлы на tmp_path."""

import threading
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

import pytest

from callscope import pipeline
from callscope.checklist import load_checklist
from callscope.config import DEFAULTS_DIR, load_config
from callscope.errors import CallScopeError
from callscope.model import CallMeta
from callscope.storage import Storage

NAME = "ivanov_2026-10-12_1530.m4a"


@pytest.fixture
def storage(tmp_path) -> Storage:
    return Storage(tmp_path / "data")


def _done(storage: Storage, raw_from_fixture, normalizer, fixture="no_need.json", name=NAME):
    """Загруженная и обработанная запись (как это делает воркер)."""
    record, _ = storage.add_upload(name, fixture.encode())
    taken = storage.next_queued()
    assert taken is not None and taken.id == record.id
    config = load_config(storage.config_path)
    checklist = load_checklist(storage.checklist_path)
    raw = raw_from_fixture(fixture)
    result = pipeline.analyze_raw(name, raw, config, checklist, normalizer)
    storage.save_result(record.id, raw, result)
    return storage.get(record.id), result


def test_data_dir_layout_and_working_yaml_copied(tmp_path):
    storage = Storage(tmp_path / "data")
    assert (tmp_path / "data" / "callscope.db").is_file()
    assert storage.audio_dir == tmp_path / "data" / "audio" and storage.audio_dir.is_dir()
    assert storage.checklist_path.read_bytes() == (DEFAULTS_DIR / "checklist.yaml").read_bytes()
    assert storage.config_path.read_bytes() == (DEFAULTS_DIR / "callscope.yaml").read_bytes()


def test_working_yaml_not_overwritten_on_restart(tmp_path):
    storage = Storage(tmp_path / "data")
    storage.checklist_path.write_text("изменено", encoding="utf-8")
    Storage(tmp_path / "data")
    assert storage.checklist_path.read_text(encoding="utf-8") == "изменено"


def test_add_upload_stores_file_by_sha_and_detects_duplicate(storage):
    record, new = storage.add_upload("Ivanov_2026-10-12_1530.MP3", b"audio-bytes")
    assert new is True
    assert record.status == "queued"
    assert record.audio_path == storage.audio_dir / f"{record.sha256}.mp3"
    assert record.audio_path.read_bytes() == b"audio-bytes"
    assert record.manager == "Ivanov" and record.matched_template
    assert record.call_datetime == datetime(2026, 10, 12, 15, 30)

    again, new_again = storage.add_upload("другое_имя.mp3", b"audio-bytes")
    assert new_again is False
    assert again.id == record.id and again.file_name == record.file_name
    assert list(storage.audio_dir.iterdir()) == [record.audio_path]


def test_add_upload_from_path(storage, tmp_path):
    source = tmp_path / "x.wav"
    source.write_bytes(b"wave")
    record, new = storage.add_upload("x.wav", source)
    assert new and record.audio_path.read_bytes() == b"wave"
    assert source.exists()


def test_queue_order_by_upload_time_and_atomic_take(storage):
    first, _ = storage.add_upload("a.mp3", b"1")
    second, _ = storage.add_upload("b.mp3", b"2")
    taken = storage.next_queued()
    assert taken.id == first.id and taken.status == "processing"
    assert storage.next_queued().id == second.id
    assert storage.next_queued() is None


def test_progress_error_retry(storage):
    record, _ = storage.add_upload("a.mp3", b"1")
    storage.next_queued()
    storage.set_progress(record.id, 0.4)
    assert storage.get(record.id).progress == pytest.approx(0.4)
    storage.save_error(record.id, "Файл повреждён.")
    failed = storage.get(record.id)
    assert failed.status == "error" and failed.error == "Файл повреждён."
    storage.retry(record.id)
    again = storage.get(record.id)
    assert again.status == "queued" and again.error is None


def test_requeue_interrupted_after_crash(tmp_path):
    storage = Storage(tmp_path / "data")
    record, _ = storage.add_upload("a.mp3", b"1")
    storage.next_queued()  # процесс «упал» посреди обработки
    restarted = Storage(tmp_path / "data")
    assert restarted.requeue_interrupted() == 1
    assert restarted.get(record.id).status == "queued"
    assert restarted.next_queued().id == record.id


def test_save_result_persists_raw_items_and_score(storage, raw_from_fixture, normalizer):
    record, result = _done(storage, raw_from_fixture, normalizer)
    assert record.status == "done" and record.progress == 1.0
    assert record.raw == raw_from_fixture("no_need.json")
    assert record.dialogue == result.dialogue
    assert record.auto_items == result.score.items
    assert record.score == result.score
    assert record.corrections == {}
    assert record.manager == "ivanov"


def test_correction_changes_score_and_loss(storage, raw_from_fixture, normalizer):
    record, result = _done(storage, raw_from_fixture, normalizer)
    # no_need (этап 1): need и next_step провалены, objection не применим → 25/70 = 35.7
    assert record.score.loss_item_id == "need"
    assert record.score.score == 35.7

    corrected = storage.set_correction(record.id, "need", "passed", "Потребность выяснена")
    assert corrected.score.score == 64.3  # (5 + 20 + 15 + 5) / 70
    assert corrected.score.loss_item_id == "next_step"
    assert corrected.score.grade == "medium"
    assert corrected.corrections["need"].status == "passed"
    assert corrected.corrections["need"].comment == "Потребность выяснена"
    assert corrected.auto_items == result.score.items  # авто-оценка не меняется
    assert storage.get(record.id).score.score == 64.3

    reverted = storage.clear_correction(record.id, "need")
    assert reverted.score == result.score and reverted.corrections == {}


def test_comment_only_correction_keeps_score(storage, raw_from_fixture, normalizer):
    record, result = _done(storage, raw_from_fixture, normalizer)
    corrected = storage.set_correction(record.id, "greeting", None, "Хорошо поздоровался")
    assert corrected.score == result.score
    assert corrected.corrections["greeting"].status is None


def test_correction_of_unknown_item_rejected(storage, raw_from_fixture, normalizer):
    record, _ = _done(storage, raw_from_fixture, normalizer)
    with pytest.raises(CallScopeError):
        storage.set_correction(record.id, "no_such_item", "passed", "")


def test_update_analysis_keeps_corrections_and_drops_removed(
    storage, raw_from_fixture, normalizer
):
    record, result = _done(storage, raw_from_fixture, normalizer)
    storage.set_correction(record.id, "need", "passed", "")
    storage.set_correction(record.id, "farewell", "failed", "")
    without_farewell = replace(
        result,
        score=replace(
            result.score, items=tuple(i for i in result.score.items if i.item_id != "farewell")
        ),
    )
    storage.update_analysis(record.id, without_farewell)
    updated = storage.get(record.id)
    assert set(updated.corrections) == {"need"}
    assert [i.item_id for i in updated.auto_items] == [
        i.item_id for i in without_farewell.score.items
    ]
    # без farewell: (5 + 20 + 15) / (5 + 20 + 15 + 25) = 61.5 (need исправлен на passed)
    assert updated.score.score == 61.5


def test_meta_and_channel_updates(storage):
    record, _ = storage.add_upload("a.mp3", b"1")
    meta = CallMeta("a.mp3", "petrova", datetime(2026, 10, 1, 9, 0), True)
    storage.update_meta(record.id, meta)
    storage.set_manager_channel(record.id, "right")
    got = storage.get(record.id)
    assert (got.manager, got.call_datetime, got.matched_template) == (
        "petrova", datetime(2026, 10, 1, 9, 0), True
    )
    assert got.manager_channel == "right"


def test_delete_removes_row_corrections_and_file(storage, raw_from_fixture, normalizer):
    record, _ = _done(storage, raw_from_fixture, normalizer)
    storage.set_correction(record.id, "need", "passed", "")
    storage.delete(record.id)
    assert storage.get(record.id) is None
    assert not record.audio_path.exists()
    assert storage.list_calls() == []
    # тот же файл можно загрузить заново
    _again, new = storage.add_upload(NAME, b"no_need.json")
    assert new is True


@dataclass(frozen=True)
class _Period:
    start: date | None
    end: date | None


@dataclass(frozen=True)
class _Filters:
    period: _Period
    manager: str | None = None
    score_min: float | None = None
    score_max: float | None = None
    failed_item: str | None = None


def test_list_calls_light_records_and_simple_filters(storage, raw_from_fixture, normalizer):
    good, _ = _done(storage, raw_from_fixture, normalizer, "good.json",
                    "ivanov_2026-10-12_1530.m4a")
    bad, _ = _done(storage, raw_from_fixture, normalizer, "no_need.json",
                   "petrova_2026-10-20_1000.m4a")
    storage.add_upload("queued_2026-10-21_1000.mp3", b"q")

    every = storage.list_calls()
    assert {r.id for r in every} == {good.id, bad.id} | {every[0].id, every[-1].id}
    assert len(every) == 3
    assert all(r.raw is None for r in every)

    def ids(**kw):
        return {r.id for r in storage.list_calls(_Filters(_Period(None, None), **kw))}

    assert ids() == {good.id, bad.id}  # только готовые
    assert ids(manager="petrova") == {bad.id}
    assert ids(score_max=90) == {bad.id}
    assert ids(score_min=90) == {good.id}
    assert ids(failed_item="need") == {bad.id}
    period = _Filters(_Period(date(2026, 10, 15), date(2026, 10, 31)))
    assert {r.id for r in storage.list_calls(period)} == {bad.id}


def test_queue_overview_newest_first(storage):
    a, _ = storage.add_upload("a.mp3", b"1")
    b, _ = storage.add_upload("b.mp3", b"2")
    assert [r.id for r in storage.queue_overview()] == [b.id, a.id]
    assert [r.id for r in storage.queue_overview(limit=1)] == [b.id]


def test_concurrent_reads_while_writing(storage):
    record, _ = storage.add_upload("a.mp3", b"1")
    storage.next_queued()
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                storage.get(record.id)
                storage.queue_overview()
        except BaseException as exc:  # noqa: BLE001 — тест собирает любые сбои
            errors.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    for i in range(200):
        storage.set_progress(record.id, i / 200)
    stop.set()
    thread.join()
    assert errors == []


def test_audio_path_is_inside_data_dir(storage):
    record, _ = storage.add_upload("../../evil.mp3", b"1")
    assert Path(record.audio_path).parent == storage.audio_dir
