"""Ошибки панели: HTML-запросы получают страницу в каркасе с тем же кодом, API — JSON."""

from __future__ import annotations

import pytest

from callscope.storage import Storage


@pytest.fixture
def queued_id(web_settings) -> int:
    return Storage(web_settings.data_dir).add_upload("queued.mp3", b"q")[0].id


def _is_error_page(response, code: int) -> bool:
    return (
        response.status_code == code
        and response.headers["content-type"].startswith("text/html")
        and '<div class="alert bad">' in response.text
        and 'href="/calls"' in response.text  # меню каркаса на месте
    )


def test_unknown_call_page_is_html_404(authed):
    response = authed.get("/calls/12345")
    assert _is_error_page(response, 404)
    assert "Звонок не найден." in response.text


def test_bad_call_id_in_path_is_html_422(authed):
    assert _is_error_page(authed.get("/calls/abc"), 422)


def test_correction_with_unknown_status_is_html_400(authed, queued_id):
    response = authed.post(f"/calls/{queued_id}/correction",
                           data={"item_id": "greeting", "status": "weird"})
    assert _is_error_page(response, 400)
    assert "Неизвестный статус пункта." in response.text


def test_swap_on_unprocessed_call_is_html_409(authed, queued_id):
    assert _is_error_page(authed.post(f"/calls/{queued_id}/swap"), 409)


def test_api_and_audio_errors_stay_json(authed):
    for response in (
        authed.get("/audio/999"),
        authed.post("/api/calls/999/retry"),
        authed.get("/audio/abc"),
    ):
        assert response.status_code in (400, 404, 422)
        assert response.headers["content-type"].startswith("application/json")
