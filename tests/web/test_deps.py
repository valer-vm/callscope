"""Общие зависимости роутеров: рабочие настройки из папки данных, фильтры шаблонов."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

from fastapi import Request

from callscope.storage import Storage
from callscope.web.deps import (
    PeriodChoice,
    WorkingSettings,
    format_date,
    format_datetime,
    format_percent,
    format_score,
    get_period_choice,
    score_grade,
    set_period_choice,
)


def test_working_settings_reload_when_file_changes(tmp_path):
    storage = Storage(tmp_path / "data")
    working = WorkingSettings(storage)
    first = working.config()
    assert working.config() is first  # без изменений — из кеша

    text = storage.config_path.read_text(encoding="utf-8")
    storage.config_path.write_text(text + "\nmodel_size: tiny\n", encoding="utf-8")
    stat = storage.config_path.stat()
    os.utime(storage.config_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert working.config().model_size == "tiny"
    config, checklist = working.load()
    assert config.model_size == "tiny" and checklist.items


def test_template_filters():
    assert format_datetime(datetime(2026, 10, 16, 14, 5)) == "16.10.2026 14:05"
    aware = datetime(2026, 10, 16, 11, 0, tzinfo=UTC)
    assert format_datetime(aware) == aware.astimezone().strftime("%d.%m.%Y %H:%M")
    assert format_date(date(2026, 1, 2)) == "02.01.2026"
    assert format_datetime(None) == "—" and format_score(None) == "—"
    assert format_score(64.3) == "64"
    assert score_grade(80) == "good" and score_grade(50) == "medium" and score_grade(49.9) == "bad"
    assert format_percent(0.375) == "38%"


def test_period_choice_survives_between_requests(app, authed):
    @app.get("/test-period-set")
    def _set(request: Request) -> dict:
        set_period_choice(request, PeriodChoice("custom", date(2026, 9, 1), None))
        return {}

    @app.get("/test-period-get")
    def _get(request: Request) -> dict:
        choice = get_period_choice(request)
        return {"preset": choice.preset, "start": str(choice.start), "end": str(choice.end)}

    first = authed.get("/test-period-get")
    assert first.status_code == 200, first.text
    assert first.json()["preset"] == "4weeks"
    authed.get("/test-period-set")
    assert authed.get("/test-period-get").json() == {
        "preset": "custom", "start": "2026-09-01", "end": "None"
    }
