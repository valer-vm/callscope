"""Обзор, менеджеры, страница менеджера и выбор периода — через TestClient."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from callscope import pipeline
from callscope.checklist import load_checklist
from callscope.config import load_config
from callscope.storage import Storage


def _add(storage: Storage, raw_from_fixture, normalizer, fixture: str, name: str):
    record, _ = storage.add_upload(name, (fixture + name).encode())
    storage.next_queued()
    config = load_config(storage.config_path)
    checklist = load_checklist(storage.checklist_path)
    raw = raw_from_fixture(fixture)
    result = pipeline.analyze_raw(name, raw, config, checklist, normalizer)
    storage.save_result(record.id, raw, result)
    return record.id


def _stamp(days_ago: int) -> str:
    day = date.today() - timedelta(days=days_ago)
    return f"{day:%Y-%m-%d}_1030"


@pytest.fixture
def seeded(web_settings, raw_from_fixture, normalizer) -> dict[str, int]:
    storage = Storage(web_settings.data_dir)
    ids = {
        "good": _add(storage, raw_from_fixture, normalizer, "good.json",
                     f"ivanov_{_stamp(3)}.m4a"),
        "bad": _add(storage, raw_from_fixture, normalizer, "no_need.json",
                    f"petrova_{_stamp(10)}.m4a"),
        "old": _add(storage, raw_from_fixture, normalizer, "no_need.json",
                    f"petrova_{_stamp(200)}.m4a"),
    }
    storage.add_upload("queued.mp3", b"q")  # в очереди — в статистику не попадает
    return ids


def test_pages_require_login(client):
    assert client.get("/").status_code == 303
    assert client.get("/managers").status_code == 303


def test_overview_empty_state_points_to_upload(authed):
    response = authed.get("/")
    assert response.status_code == 200
    assert "Звонков за этот период нет" in response.text
    assert 'href="/upload"' in response.text


def test_overview_shows_numbers_funnel_and_chart(authed, seeded):
    html = authed.get("/").text  # по умолчанию 4 недели: good и bad, old не входит
    assert "Звонков" in html and "Средний балл" in html
    assert "ниже 80" in html
    assert "<svg" in html and "Воронка" in html
    assert "Чаще всего звонок проваливается на пункте" in html
    # средний балл (100 + 35.7) / 2 ≈ 68
    assert ">68<" in html


def test_period_choice_is_remembered_across_pages(authed, seeded):
    assert "Последние 4 недели" in authed.get("/").text
    response = authed.post("/period", data={"preset": "all", "next": "/managers"})
    assert response.status_code == 303 and response.headers["location"] == "/managers"
    managers = authed.get("/managers").text
    assert "Всё время" in managers
    assert "petrova" in managers and "2" in managers  # старый звонок теперь виден
    assert "Всё время" in authed.get("/").text


def test_custom_period_and_open_redirect_is_blocked(authed, seeded):
    start = date.today() - timedelta(days=5)
    response = authed.post(
        "/period",
        data={"preset": "custom", "start": start.isoformat(), "end": date.today().isoformat(),
              "next": "https://evil.example/"},
    )
    assert response.headers["location"] == "/"
    html = authed.get("/").text
    assert f"{start:%d.%m.%Y}" in html
    assert "Звонков за этот период нет" not in html
    managers = authed.get("/managers").text
    assert "ivanov" in managers and "petrova" not in managers


def test_managers_ranking_order_and_links(authed, seeded):
    authed.post("/period", data={"preset": "all", "next": "/managers"})
    html = authed.get("/managers").text
    assert html.index("ivanov") < html.index("petrova")  # 100 выше 35.7
    assert 'href="/managers/ivanov"' in html


def test_manager_page_has_chart_failures_and_call_links(authed, seeded):
    html = authed.get("/managers/petrova").text
    assert "<svg" in html and f"/calls/{seeded['bad']}" in html
    assert "Частые провалы" in html
    missing = authed.get("/managers/nobody")
    assert missing.status_code == 404
