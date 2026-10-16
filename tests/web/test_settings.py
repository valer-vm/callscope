"""«Настройки»: редактор чек-листа, переоценка, шаблон имени файла, смысловая проверка."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from callscope.checklist import ChecklistItem, load_checklist
from callscope.config import load_config
from callscope.semantic import OllamaStatus
from callscope.storage import Storage
from callscope.web import create_app
from callscope.web.routes import settings as settings_routes
from callscope.web.routes.settings import ITEM_FIELDS, item_view, synonyms_to_text
from callscope.worker import Worker

FIELDS = ITEM_FIELDS


def _form_of(checklist) -> dict[str, list[str]]:
    """Данные формы так, как их шлёт страница: поля пунктов списками, по порядку карточек."""
    views = [item_view(item) for item in checklist.items]
    form: dict[str, list[str]] = {name: [view[name] for view in views] for name in FIELDS}
    form["global_synonyms"] = [synonyms_to_text(checklist.synonyms)]
    return form


@pytest.fixture
def storage(web_settings) -> Storage:
    return Storage(web_settings.data_dir)


@pytest.fixture
def current(storage):
    return load_checklist(storage.checklist_path)


def test_pages_need_login(client):
    assert client.get("/settings/checklist").status_code == 303
    assert client.get("/settings/filename").status_code == 303
    assert client.post("/settings/checklist", data={}).status_code == 303
    assert client.get("/api/rescore").status_code == 401


def test_checklist_page_shows_items_and_hints(authed, current):
    page = authed.get("/settings/checklist")
    assert page.status_code == 200
    for item in current.items:
        assert item.title in page.text
    for word in ("Ключевые фразы", "Окно поиска", "Правило проверки", "Триггеры", "Синонимы"):
        assert word in page.text


def test_save_changed_weight_and_phrases_writes_working_file(authed, storage, current):
    form = _form_of(current)
    form["weight"][0] = "7,5"
    form["phrases"][0] = "здравствуйте\n  добрый день \n\nалло"
    response = authed.post("/settings/checklist", data=form)
    assert response.status_code == 303
    saved = load_checklist(storage.checklist_path)
    assert saved.items[0].weight == 7.5
    assert saved.items[0].phrases == ("здравствуйте", "добрый день", "алло")
    assert saved.items[1:] == current.items[1:]
    assert saved.synonyms == current.synonyms
    assert "Чек-лист сохранён" in authed.get("/settings/checklist?saved=1").text


def test_synonyms_one_per_line(authed, storage, current):
    form = _form_of(current)
    form["synonyms"][0] = "цена: стоимость, расценка\nскидка: акция"
    form["global_synonyms"] = ["замер: обмер"]
    assert authed.post("/settings/checklist", data=form).status_code == 303
    saved = load_checklist(storage.checklist_path)
    assert saved.items[0].synonyms == {"цена": ("стоимость", "расценка"), "скидка": ("акция",)}
    assert saved.synonyms == {"замер": ("обмер",)}


def test_bad_weight_shows_message_at_field_and_keeps_file(authed, storage, current):
    before = storage.checklist_path.read_bytes()
    form = _form_of(current)
    form["weight"][1] = "много"
    response = authed.post("/settings/checklist", data=form)
    assert response.status_code == 400
    assert "нужно число больше нуля" in response.text
    assert "файл не изменён" in response.text
    assert 'value="много"' in response.text  # введённое не потеряно
    assert storage.checklist_path.read_bytes() == before


def test_bad_synonym_line_is_reported(authed, storage, current):
    before = storage.checklist_path.read_bytes()
    form = _form_of(current)
    form["synonyms"][2] = "слово без двоеточия"
    response = authed.post("/settings/checklist", data=form)
    assert response.status_code == 400
    assert "слово без двоеточия" in response.text
    assert storage.checklist_path.read_bytes() == before


def test_item_without_phrases_is_rejected(authed, storage, current):
    before = storage.checklist_path.read_bytes()
    form = _form_of(current)
    form["phrases"][0] = ""
    form["statement_phrases"][0] = ""
    response = authed.post("/settings/checklist", data=form)
    assert response.status_code == 400
    assert "хотя бы одна ключевая фраза" in response.text
    assert storage.checklist_path.read_bytes() == before


def test_add_delete_and_reorder(authed, storage, current):
    form = _form_of(current)
    removed_id = form["id"].pop(1)
    for name in FIELDS:
        if name != "id":
            form[name].pop(1)
    for name in FIELDS:  # первый и последний пункты меняются местами
        form[name][0], form[name][-1] = form[name][-1], form[name][0]
    new = {"id": "", "title": "Прощание", "weight": "3", "speaker": "manager", "window": "last",
           "rule": "phrases", "phrases": "до свидания\nвсего доброго", "statement_phrases": "",
           "synonyms": "", "window_size": "2", "min_matches": "1", "trigger": "",
           "respond_within": "2", "description": ""}
    for name in FIELDS:
        form[name].append(new[name])
    assert authed.post("/settings/checklist", data=form).status_code == 303

    saved = load_checklist(storage.checklist_path)
    ids = [item.id for item in saved.items]
    assert removed_id not in ids
    assert ids[0] == current.items[-1].id and ids[-2] == current.items[0].id
    farewell = saved.items[-1]
    assert isinstance(farewell, ChecklistItem)
    assert farewell.id.startswith("item_") and farewell.window == "last"
    assert farewell.window_size == 2 and farewell.phrases == ("до свидания", "всего доброго")


def test_empty_checklist_is_rejected(authed, storage):
    before = storage.checklist_path.read_bytes()
    response = authed.post("/settings/checklist", data={"global_synonyms": ""})
    assert response.status_code == 400
    assert storage.checklist_path.read_bytes() == before


def test_builtin_files_are_not_touched(authed, current):
    from callscope.config import DEFAULTS_DIR

    before = (DEFAULTS_DIR / "checklist.yaml").read_bytes()
    form = _form_of(current)
    form["weight"][0] = "9"
    authed.post("/settings/checklist", data=form)
    assert (DEFAULTS_DIR / "checklist.yaml").read_bytes() == before


# --- переоценка ------------------------------------------------------------------------


class FakeWorker:
    def __init__(self):
        from callscope.worker import RescoreStatus

        self.status = RescoreStatus("idle")
        self.requested = 0

    def request_rescore(self):
        from callscope.worker import RescoreStatus

        self.requested += 1
        self.status = RescoreStatus("running")

    @property
    def rescore_status(self):
        return self.status

    def start(self):
        pass

    def stop(self, timeout=5):
        pass


def test_rescore_without_worker_is_unavailable(authed):
    response = authed.post("/settings/rescore", headers={"accept": "application/json"})
    assert response.status_code == 503
    assert authed.get("/api/rescore").json()["state"] == "unavailable"


def test_rescore_progress_and_finish_message(web_settings, login):
    from datetime import UTC, datetime

    from callscope.worker import RescoreStatus

    fake = FakeWorker()
    client = TestClient(create_app(web_settings, worker=fake), follow_redirects=False)
    login(client)
    assert client.get("/api/rescore").json()["state"] == "idle"

    started = client.post("/settings/rescore", headers={"accept": "application/json"})
    assert started.status_code == 202 and fake.requested == 1
    # второе нажатие, пока идёт переоценка, новую задачу не ставит
    client.post("/settings/rescore", headers={"accept": "application/json"})
    assert fake.requested == 1

    fake.status = RescoreStatus("running", 3, 12)
    assert "3 из 12" in client.get("/api/rescore").json()["message"]
    assert "3 из 12" in client.get("/settings/checklist").text

    fake.status = RescoreStatus("done", 12, 12, datetime(2026, 10, 16, 12, 0, tzinfo=UTC))
    payload = client.get("/api/rescore").json()
    assert payload["state"] == "done" and "завершено" in payload["message"]
    assert payload["finished_at"].startswith("2026-10-16")


def test_html_rescore_post_redirects_back(web_settings, login):
    client = TestClient(create_app(web_settings, worker=FakeWorker()), follow_redirects=False)
    login(client)
    response = client.post("/settings/rescore")
    assert response.status_code == 303 and response.headers["location"].startswith("/settings/")


def test_rescore_applies_new_checklist_without_transcribing(
    web_settings, login, fake_transcriber_cls
):
    storage = Storage(web_settings.data_dir)
    transcriber = fake_transcriber_cls()
    worker = Worker(
        storage,
        lambda config: transcriber,
        lambda: (load_config(storage.config_path), load_checklist(storage.checklist_path)),
    )
    call, _ = storage.add_upload("ivanov_2026-10-12_1530.m4a", b"no_need.json")
    assert worker.run_once() is True
    score_before = storage.get(call.id).score.score
    transcribed = list(transcriber.calls)

    client = TestClient(create_app(web_settings, worker=worker), follow_redirects=False)
    login(client)
    form = _form_of(load_checklist(storage.checklist_path))
    form["weight"] = ["1"] * len(form["weight"])
    form["weight"][0] = "50"
    assert client.post("/settings/checklist", data=form).status_code == 303

    started = client.post("/settings/rescore", headers={"accept": "application/json"})
    assert started.status_code == 202
    assert worker.run_once() is True
    status = client.get("/api/rescore").json()
    assert status["state"] == "done" and (status["done"], status["total"]) == (1, 1)
    assert storage.get(call.id).score.score != score_before
    assert transcriber.calls == transcribed  # расшифровка не вызывалась


# --- имя файла -------------------------------------------------------------------------


def test_filename_page_shows_current_settings_and_example_result(authed):
    page = authed.get("/settings/filename")
    assert page.status_code == 200
    assert "{manager}_{date}_{time}" in page.text
    assert "Менеджер: ivanov" in page.text and "12.10.2026 15:30" in page.text


def test_filename_check_endpoint(authed):
    data = {"template": "{date}-{manager}", "date_format": "%d.%m.%Y", "time_format": "%H%M",
            "example": "12.10.2026-petrov.mp3"}
    answer = authed.post("/settings/filename/check", data=data).json()
    assert answer["matched"] is True and answer["manager"] == "petrov"
    assert "12.10.2026" in answer["message"]
    data["example"] = "ivanov_2026-10-12_1530.mp3"
    miss = authed.post("/settings/filename/check", data=data).json()
    assert miss["matched"] is False and "не подошло" in miss["message"]


def test_filename_check_button_without_saving(authed, storage):
    before = storage.config_path.read_bytes()
    data = {"template": "{manager}", "date_format": "%Y", "time_format": "%H", "example": "a.mp3",
            "action": "check"}
    response = authed.post("/settings/filename", data=data)
    assert response.status_code == 200 and "Менеджер: a" in response.text
    assert storage.config_path.read_bytes() == before


def test_filename_save_updates_config_and_recalculates_calls(authed, storage):
    matching, _ = storage.add_upload("12.10.2026-petrov.mp3", b"a")
    other, _ = storage.add_upload("ivanov_2026-10-12_1530.mp3", b"b")
    old_meta = storage.get(other.id)
    data = {"template": "{date}-{manager}", "date_format": "%d.%m.%Y", "time_format": "%H%M",
            "example": "12.10.2026-petrov.mp3", "action": "save"}
    response = authed.post("/settings/filename", data=data)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/filename?updated=1&unmatched=1"

    config = load_config(storage.config_path)
    assert config.filename_template == "{date}-{manager}" and config.date_format == "%d.%m.%Y"
    assert storage.get(matching.id).manager == "petrov"
    assert storage.get(matching.id).call_datetime.day == 12
    after = storage.get(other.id)  # не подошло — прежние значения
    assert (after.manager, after.call_datetime) == (old_meta.manager, old_meta.call_datetime)

    page = authed.get(response.headers["location"])
    assert "Обновлено звонков: <b>1</b>" in page.text
    assert "не подошло под шаблон: <b>1</b>" in page.text


def test_filename_save_rejects_empty_template(authed, storage):
    before = storage.config_path.read_bytes()
    data = {"template": "  ", "date_format": "%Y", "time_format": "%H", "example": "x.mp3",
            "action": "save"}
    response = authed.post("/settings/filename", data=data)
    assert response.status_code == 400 and "Введите шаблон" in response.text
    assert storage.config_path.read_bytes() == before


# --- смысловая проверка --------------------------------------------------------------


@pytest.fixture
def ollama(monkeypatch):
    """Состояние Ollama для страницы: по умолчанию работает, скачана qwen2.5:3b."""
    state = {"status": OllamaStatus(True, ("qwen2.5:3b",)), "urls": []}

    def status(url):
        state["urls"].append(url)
        return state["status"]

    monkeypatch.delenv("CALLSCOPE_OLLAMA_URL", raising=False)
    monkeypatch.setattr(settings_routes, "ollama_status", status)
    return state


def test_semantic_page_needs_login(client):
    assert client.get("/settings/semantic").status_code == 303


def test_semantic_page_shows_status_and_missing_model(authed, ollama):
    page = authed.get("/settings/semantic")
    assert page.status_code == 200
    assert "Скачанные модели: qwen2.5:3b" in page.text
    # по умолчанию выбрана qwen2.5:7b — её нет среди скачанных
    assert "ollama pull qwen2.5:7b" in page.text
    assert 'name="enabled" value="1">' in page.text  # выключено


def test_semantic_page_when_ollama_is_down(authed, ollama):
    ollama["status"] = OllamaStatus(False, error="Connection refused")
    page = authed.get("/settings/semantic")
    assert "Ollama не отвечает" in page.text and "Connection refused" in page.text


def test_semantic_save_enables_and_changes_model(authed, storage, ollama):
    response = authed.post("/settings/semantic", data={"enabled": "1", "model": "qwen2.5:3b"})
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/semantic?saved=1"
    semantic = load_config(storage.config_path).semantic
    assert semantic.enabled is True and semantic.model == "qwen2.5:3b"
    page = authed.get(response.headers["location"])
    assert "Новые звонки будут проверяться моделью" in page.text
    assert "не скачана" not in page.text

    authed.post("/settings/semantic", data={"model": "qwen2.5:3b"})
    assert load_config(storage.config_path).semantic.enabled is False


def test_semantic_save_rejects_empty_model(authed, storage, ollama):
    before = storage.config_path.read_bytes()
    response = authed.post("/settings/semantic", data={"enabled": "1", "model": " "})
    assert response.status_code == 400 and "Укажите модель" in response.text
    assert storage.config_path.read_bytes() == before


def test_settings_tabs_link_to_semantic(authed, ollama):
    for url in ("/settings/checklist", "/settings/filename", "/settings/semantic"):
        assert 'href="/settings/semantic"' in authed.get(url).text


def test_semantic_page_uses_address_from_environment(authed, ollama, monkeypatch):
    monkeypatch.setenv("CALLSCOPE_OLLAMA_URL", "http://ollama:11434")
    ollama["status"] = OllamaStatus(False, error="Connection refused")
    page = authed.get("/settings/semantic")
    assert ollama["urls"] == ["http://ollama:11434"]
    assert "http://ollama:11434" in page.text and "Адрес задан в окружении" in page.text
