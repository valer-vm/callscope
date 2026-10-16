"""Приём записей, очередь, «Повторить», отдача записи с Range."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from callscope.checklist import load_checklist
from callscope.config import load_config
from callscope.web import create_app
from callscope.web.routes import upload as upload_routes
from callscope.worker import Worker


def _send(client, name: str, content: bytes):
    return client.post("/api/upload", files={"file": (name, content, "application/octet-stream")})


def test_upload_accepts_and_queues(authed, app):
    response = _send(authed, "ivanov_2026-10-12_1530.m4a", b"good.json")
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "queued" and data["duplicate"] is False
    record = app.state.storage.get(data["id"])
    assert record.file_name == "ivanov_2026-10-12_1530.m4a"
    assert record.manager == "ivanov"
    assert record.audio_path.read_bytes() == b"good.json"


def test_upload_keeps_only_base_name(authed, app):
    data = _send(authed, "C:\\calls\\petrova_2026-10-13_1015.MP3", b"x").json()
    assert app.state.storage.get(data["id"]).file_name == "petrova_2026-10-13_1015.MP3"


def test_duplicate_returns_existing_call(authed, app):
    first = _send(authed, "a.mp3", b"same content").json()
    second = _send(authed, "b.wav", b"same content")
    assert second.status_code == 200
    data = second.json()
    assert data["duplicate"] is True and data["id"] == first["id"]
    assert data["url"] == f"/calls/{first['id']}"
    assert "уже загружена" in data["message"]
    assert len(app.state.storage.list_calls()) == 1


@pytest.mark.parametrize(
    ("name", "content", "status", "reason"),
    [
        ("notes.txt", b"abc", 415, "не тот формат"),
        ("call.mp4", b"abc", 415, "не тот формат"),
        ("empty.mp3", b"", 400, "пустой"),
    ],
)
def test_bad_files_are_rejected_with_reason(authed, app, name, content, status, reason):
    response = _send(authed, name, content)
    assert response.status_code == status
    data = response.json()
    assert data["status"] == "rejected" and data["id"] is None
    assert reason in data["message"]
    assert app.state.storage.list_calls() == []


def test_too_big_file_is_rejected(authed, app, monkeypatch):
    monkeypatch.setattr(upload_routes, "MAX_UPLOAD_BYTES", 10)
    response = _send(authed, "big.ogg", b"x" * 11)
    assert response.status_code == 413
    assert "больше" in response.json()["message"]
    assert app.state.storage.list_calls() == []
    # временный файл не остаётся в папке данных
    assert not list(app.state.storage.data_dir.glob(".incoming-*"))


def test_queue_shows_status_progress_score_and_error(authed, app, fake_transcriber_cls):
    storage = app.state.storage
    good = _send(authed, "ivanov_2026-10-12_1530.m4a", b"good.json").json()
    broken = _send(authed, "broken.ogg", b"BROKEN").json()
    waiting = _send(authed, "petrova_2026-10-13_1015.mp3", b"no_need.json").json()

    worker = Worker(
        storage,
        lambda config: fake_transcriber_cls(),
        lambda: (load_config(storage.config_path), load_checklist(storage.checklist_path)),
    )
    worker.run_once()
    worker.run_once()
    storage.next_queued()
    storage.set_progress(waiting["id"], 0.42)

    data = authed.get("/api/queue").json()
    items = {item["id"]: item for item in data["items"]}
    assert items[good["id"]]["status"] == "done"
    assert items[good["id"]]["score_text"] == "100"
    assert items[good["id"]]["grade"] == "good"
    assert items[broken["id"]]["status"] == "error"
    assert "повреждён" in items[broken["id"]]["error"]
    assert items[waiting["id"]]["status_text"] == "обрабатывается 42%"
    assert data["counts"] == {"queued": 0, "processing": 1, "done": 1, "error": 1}
    # новые сверху
    assert [i["id"] for i in data["items"]] == [waiting["id"], broken["id"], good["id"]]


def test_retry_requeues_error_only(authed, app):
    storage = app.state.storage
    call = _send(authed, "a.mp3", b"BROKEN").json()
    assert authed.post(f"/api/calls/{call['id']}/retry").status_code == 409
    storage.next_queued()
    storage.save_error(call["id"], "Файл повреждён.")
    response = authed.post(f"/api/calls/{call['id']}/retry")
    assert response.status_code == 200
    assert storage.get(call["id"]).status == "queued"
    assert authed.post("/api/calls/999/retry").status_code == 404


def test_upload_wakes_worker_only_for_new_file(web_settings):
    class SpyWorker:
        woken = 0

        def wake(self):
            self.woken += 1

    spy = SpyWorker()
    client = TestClient(create_app(web_settings, worker=spy), follow_redirects=False)
    client.post("/login", data={"password": web_settings.password})
    _send(client, "a.mp3", b"one")
    _send(client, "b.mp3", b"one")  # дубликат — будить незачем
    assert spy.woken == 1


def test_audio_supports_range(authed):
    payload = bytes(range(256)) * 4
    call = _send(authed, "a.m4a", payload).json()
    full = authed.get(f"/audio/{call['id']}")
    assert full.status_code == 200 and full.content == payload
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-type"] == "audio/mp4"
    part = authed.get(f"/audio/{call['id']}", headers={"Range": "bytes=10-19"})
    assert part.status_code == 206
    assert part.content == payload[10:20]
    assert part.headers["content-range"] == f"bytes 10-19/{len(payload)}"
    assert authed.get("/audio/999").status_code == 404


def test_upload_page_has_dropzone_and_script(authed):
    page = authed.get("/upload")
    assert page.status_code == 200
    assert 'id="dropzone"' in page.text and "/static/upload.js" in page.text
    assert 'class="active"' in page.text


def test_upload_without_content_length_is_refused_before_reading(authed):
    """Потоковое тело без длины не пишется на диск целиком — сразу 411."""
    def body():
        yield b"--x\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp3\"\r\n\r\n"
        yield b"0" * 1024
        yield b"\r\n--x--\r\n"

    response = authed.post(
        "/api/upload", content=body(),
        headers={"content-type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 411
    assert response.json()["status"] == "rejected"
