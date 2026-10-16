"""Фоновый воркер: очередь по одной записи, ошибки, восстановление, переоценка."""

import threading
import time

import pytest

from callscope.checklist import load_checklist
from callscope.config import load_config
from callscope.errors import CallScopeError
from callscope.storage import Storage
from callscope.worker import Worker


@pytest.fixture
def storage(tmp_path) -> Storage:
    return Storage(tmp_path / "data")


def _worker(storage: Storage, transcriber):
    created: list[str] = []

    def factory(config):
        created.append(config.model_size)
        return transcriber

    def load_settings():
        return load_config(storage.config_path), load_checklist(storage.checklist_path)

    worker = Worker(storage, factory, load_settings)
    worker.created = created  # type: ignore[attr-defined]
    return worker


def test_run_once_processes_in_upload_order(storage, fake_transcriber_cls):
    transcriber = fake_transcriber_cls()
    first, _ = storage.add_upload("ivanov_2026-10-12_1530.m4a", b"good.json")
    second, _ = storage.add_upload("petrova_2026-10-13_1015.mp3", b"no_need.json")
    worker = _worker(storage, transcriber)

    assert worker.run_once() is True
    assert storage.get(first.id).status == "done"
    assert storage.get(second.id).status == "queued"
    assert worker.run_once() is True
    assert worker.run_once() is False

    done = storage.get(second.id)
    assert done.status == "done" and done.raw is not None
    assert done.score.score == 35.7  # no_need, как в этапе 1
    assert storage.get(first.id).score.score == 100.0
    assert transcriber.calls == [first.audio_path.name, second.audio_path.name]
    assert len(worker.created) == 1  # модель создаётся один раз


def test_progress_is_saved(storage, fake_transcriber_cls):
    seen: list[float] = []

    class Spy(fake_transcriber_cls):
        def transcribe(self, path, mono_turn_gap, on_progress=None):
            def report(fraction):
                on_progress(fraction)
                seen.append(storage.get(record.id).progress)

            return super().transcribe(path, mono_turn_gap, report)

    record, _ = storage.add_upload("a.mp3", b"good.json")
    _worker(storage, Spy()).run_once()
    assert seen == [pytest.approx(0.5)]


def test_audio_error_does_not_stop_queue_and_retry(storage, fake_transcriber_cls):
    transcriber = fake_transcriber_cls()
    broken, _ = storage.add_upload("broken.ogg", b"BROKEN")
    good, _ = storage.add_upload("good.mp3", b"good.json")
    worker = _worker(storage, transcriber)
    worker.run_once()
    worker.run_once()
    failed = storage.get(broken.id)
    assert failed.status == "error" and "повреждён" in failed.error
    assert storage.get(good.id).status == "done"

    storage.retry(broken.id)
    assert storage.get(broken.id).status == "queued"
    broken.audio_path.write_text("good.json", encoding="utf-8")
    worker.run_once()
    assert storage.get(broken.id).status == "done"


def test_model_error_marks_call_error(storage, fake_transcriber_cls):
    class NoModel(fake_transcriber_cls):
        def transcribe(self, path, mono_turn_gap, on_progress=None):
            raise CallScopeError("Не удалось загрузить модель распознавания.")

    record, _ = storage.add_upload("a.mp3", b"good.json")
    _worker(storage, NoModel()).run_once()
    got = storage.get(record.id)
    assert got.status == "error" and "модель" in got.error


def test_duplicate_upload_is_not_processed_again(storage, fake_transcriber_cls):
    transcriber = fake_transcriber_cls()
    storage.add_upload("a.mp3", b"good.json")
    worker = _worker(storage, transcriber)
    worker.run_once()
    _record, new = storage.add_upload("a-copy.mp3", b"good.json")
    assert new is False
    assert worker.run_once() is False
    assert len(transcriber.calls) == 1


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_restart_resumes_interrupted_call(tmp_path, fake_transcriber_cls):
    crashed = Storage(tmp_path / "data")
    record, _ = crashed.add_upload("a.mp3", b"good.json")
    other, _ = crashed.add_upload("b.mp3", b"no_need.json")
    assert crashed.next_queued().id == record.id  # процесс «упал» здесь

    storage = Storage(tmp_path / "data")  # перезапуск
    worker = _worker(storage, fake_transcriber_cls())
    worker.start()
    try:
        assert _wait(
            lambda: storage.get(record.id).status == "done"
            and storage.get(other.id).status == "done"
        )
    finally:
        worker.stop()


def test_reads_during_run_once_from_other_thread(storage, fake_transcriber_cls):
    for i in range(5):
        storage.add_upload(f"m{i}_2026-10-1{i}_1000.mp3", f"good.json\n{i}".encode())

    class Slow(fake_transcriber_cls):
        def transcribe(self, path, mono_turn_gap, on_progress=None):
            for step in range(10):
                on_progress(step / 10)
            return super().transcribe(path, mono_turn_gap, None)

    worker = _worker(storage, Slow())
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                storage.list_calls()
                storage.queue_overview()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    while worker.run_once():
        pass
    stop.set()
    thread.join()
    assert errors == []
    assert all(r.status == "done" for r in storage.list_calls())


def test_request_rescore_runs_between_calls(storage, fake_transcriber_cls):
    transcriber = fake_transcriber_cls()
    storage.add_upload("a.mp3", b"no_need.json")
    worker = _worker(storage, transcriber)
    worker.run_once()
    assert worker.rescore_status.state == "idle"
    worker.request_rescore()
    assert worker.run_once() is True
    status = worker.rescore_status
    assert status.state == "done" and (status.done, status.total) == (1, 1)
    assert status.finished_at is not None
    assert len(transcriber.calls) == 1


def test_request_rescore_and_status_do_not_wait_for_transcription(
    storage, fake_transcriber_cls
):
    started, release = threading.Event(), threading.Event()

    class Blocking(fake_transcriber_cls):
        def transcribe(self, path, mono_turn_gap, on_progress=None):
            started.set()
            release.wait(10)
            return super().transcribe(path, mono_turn_gap, on_progress)

    storage.add_upload("a.mp3", b"good.json")
    worker = _worker(storage, Blocking())
    runner = threading.Thread(target=worker.run_once)
    runner.start()
    try:
        assert started.wait(5)
        began = time.monotonic()
        worker.request_rescore()
        status = worker.rescore_status
        assert time.monotonic() - began < 0.5
        assert status.state == "running"
    finally:
        release.set()
        runner.join(5)
    assert worker.run_once() is True  # переоценка выполняется после записи
    assert worker.rescore_status.state == "done"


def test_unexpected_rescore_failure_reported_and_worker_continues(
    storage, fake_transcriber_cls, monkeypatch
):
    from callscope import worker as worker_module

    def boom(*args, **kwargs):
        raise RuntimeError("сломалось")

    monkeypatch.setattr(worker_module, "rescore_all", boom)
    worker = _worker(storage, fake_transcriber_cls())
    worker.request_rescore()
    assert worker.run_once() is True
    status = worker.rescore_status
    assert status.state == "done" and status.error and status.finished_at is not None

    record, _ = storage.add_upload("a.mp3", b"good.json")
    worker.start()
    try:
        assert _wait(lambda: storage.get(record.id).status == "done")
    finally:
        worker.stop()


def test_rescore_failure_in_thread_does_not_kill_worker(
    storage, fake_transcriber_cls, monkeypatch
):
    from callscope import worker as worker_module

    monkeypatch.setattr(
        worker_module, "rescore_all", lambda *a, **k: (_ for _ in ()).throw(ValueError("x"))
    )
    worker = _worker(storage, fake_transcriber_cls())
    worker.start()
    try:
        worker.request_rescore()
        assert _wait(lambda: worker.rescore_status.state == "done")
        assert worker.rescore_status.error
        record, _ = storage.add_upload("b.mp3", b"no_need.json")
        worker.wake()
        assert _wait(lambda: storage.get(record.id).status == "done")
    finally:
        worker.stop()
