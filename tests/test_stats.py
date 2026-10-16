"""Статистика: фильтры, обзор, воронка, рейтинг, недели — на собранных CallRecord."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from callscope import stats
from callscope.checklist import Checklist, ChecklistItem
from callscope.config import Thresholds
from callscope.model import CallScore, ItemResult
from callscope.scoring import summarize
from callscope.storage import CallRecord, Correction

CHECKLIST = Checklist(
    items=(
        ChecklistItem(id="greet", title="Приветствие", weight=1),
        ChecklistItem(id="need", title="Потребность", weight=2),
        ChecklistItem(id="close", title="Следующий шаг", weight=1),
    )
)
THRESHOLDS = Thresholds(good=80, medium=50)
TODAY = date(2026, 10, 16)  # пятница


def _call(
    call_id: int,
    day: date | None,
    failed: tuple[str, ...] = (),
    manager: str = "ivanov",
    status: str = "done",
    uploaded: datetime | None = None,
) -> CallRecord:
    items = tuple(
        ItemResult(i.id, "failed" if i.id in failed else "passed", "", ()) for i in CHECKLIST.items
    )
    score: CallScore | None = summarize(items, CHECKLIST, THRESHOLDS)
    if status != "done":
        score = None
    return CallRecord(
        id=call_id, sha256=str(call_id), file_name=f"{call_id}.mp3", audio_path=Path("x"),
        uploaded_at=uploaded or datetime(2026, 10, 16, 9, 0, tzinfo=UTC), status=status,
        progress=1.0, error=None, manager=manager,
        call_datetime=datetime(day.year, day.month, day.day, 12, 0) if day else None,
        matched_template=day is not None, manager_channel="auto", raw=None, dialogue=None,
        auto_items=items, corrections={}, score=score, warnings=(),
    )


def _period(start: date | None, end: date | None) -> stats.Period:
    return stats.Period(start, end, "тест")


def test_period_presets():
    assert stats.period_preset("week", TODAY).start == date(2026, 10, 10)
    assert stats.period_preset("4weeks", TODAY).start == date(2026, 9, 19)
    assert stats.period_preset("quarter", TODAY).start == date(2026, 7, 19)
    for name in ("week", "4weeks", "quarter"):
        assert stats.period_preset(name, TODAY).end == TODAY
    everything = stats.period_preset("all", TODAY)
    assert (everything.start, everything.end) == (None, None)
    assert stats.period_preset("4weeks", TODAY).label == "Последние 4 недели"


def test_apply_filters_only_done_and_by_fields():
    calls = [
        _call(1, date(2026, 10, 12)),
        _call(2, date(2026, 10, 13), failed=("need",), manager="petrova"),
        _call(3, date(2026, 10, 14), status="queued"),
        _call(4, date(2026, 9, 1)),
    ]

    def ids(**kw):
        period = kw.pop("period", _period(None, None))
        return [c.id for c in stats.apply_filters(calls, stats.CallFilters(period, **kw))]

    assert ids() == [1, 2, 4]  # очередь не считается
    assert ids(period=_period(date(2026, 10, 1), None)) == [1, 2]
    assert ids(period=_period(None, date(2026, 10, 12))) == [1, 4]
    assert ids(manager="petrova") == [2]
    assert ids(score_max=70) == [2]  # need вес 2 из 4 → 50
    assert ids(score_min=90) == [1, 4]
    assert ids(failed_item="need") == [2]


def test_call_without_date_uses_local_upload_date():
    uploaded = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
    call = _call(1, None, uploaded=uploaded)
    expected = uploaded.astimezone().date()
    inside = stats.CallFilters(_period(expected, expected))
    assert stats.apply_filters([call], inside) == [call]
    outside = stats.CallFilters(_period(date(2020, 1, 1), date(2020, 1, 2)))
    assert stats.apply_filters([call], outside) == []


def test_overview_numbers_and_funnel_in_checklist_order():
    calls = [
        _call(1, date(2026, 10, 12)),  # 100
        _call(2, date(2026, 10, 13), failed=("need",)),  # 50, потеряно на need
        _call(3, date(2026, 10, 14), failed=("greet", "need")),  # 25, потеряно на greet
        _call(4, date(2026, 10, 15), failed=("need",)),  # 50
    ]
    result = stats.overview(calls, CHECKLIST, THRESHOLDS, _period(None, None))
    assert result.calls == 4
    assert result.avg_score == pytest.approx(56.25, abs=0.06)  # (100+50+25+50)/4
    assert result.below_good_share == 0.75
    assert result.good_threshold == 80
    assert [s.item_id for s in result.funnel] == ["greet", "need", "close"]
    assert [(s.failed, s.lost) for s in result.funnel] == [(1, 1), (3, 2), (0, 0)]
    assert result.funnel[1].title == "Потребность"
    assert result.top_failure is not None and result.top_failure.item_id == "need"


def test_overview_empty_has_no_division_by_zero():
    result = stats.overview([], CHECKLIST, THRESHOLDS, _period(None, None))
    assert result.calls == 0 and result.avg_score is None and result.below_good_share is None
    assert result.top_failure is None and result.weekly == ()
    assert [s.failed for s in result.funnel] == [0, 0, 0]


def test_overview_no_failures_has_no_top_failure():
    result = stats.overview([_call(1, date(2026, 10, 12))], CHECKLIST, THRESHOLDS,
                            _period(None, None))
    assert result.top_failure is None


def test_overview_respects_period_and_ignores_unfinished():
    calls = [
        _call(1, date(2026, 10, 12)),
        _call(2, date(2026, 8, 1), failed=("need",)),
        _call(3, date(2026, 10, 13), status="processing"),
    ]
    result = stats.overview(calls, CHECKLIST, THRESHOLDS, _period(date(2026, 10, 1), TODAY))
    assert result.calls == 1 and result.avg_score == 100.0


def test_manual_correction_changes_numbers():
    base = _call(1, date(2026, 10, 12), failed=("need",))  # 50
    corrected_items = tuple(
        ItemResult(i.item_id, "passed", i.reason, ()) if i.item_id == "need" else i
        for i in base.auto_items
    )
    corrected = CallRecord(
        **{**base.__dict__, "score": summarize(corrected_items, CHECKLIST, THRESHOLDS),
           "corrections": {"need": Correction("need", "passed", "", base.uploaded_at)}}
    )
    before = stats.overview([base], CHECKLIST, THRESHOLDS, _period(None, None))
    after = stats.overview([corrected], CHECKLIST, THRESHOLDS, _period(None, None))
    assert (before.avg_score, after.avg_score) == (50.0, 100.0)
    assert before.top_failure is not None and after.top_failure is None


def test_manager_ranking_sorted_with_top_failures():
    calls = [
        _call(1, date(2026, 10, 12), manager="ivanov"),
        _call(2, date(2026, 10, 13), manager="petrova", failed=("need",)),
        _call(3, date(2026, 10, 14), manager="petrova", failed=("need", "close")),
        _call(4, date(2026, 10, 14), manager="petrova", failed=("greet",)),
        _call(5, date(2026, 10, 14), manager="sidorov", failed=("greet", "need", "close")),
    ]
    rows = stats.manager_ranking(calls, CHECKLIST)
    assert [r.manager for r in rows] == ["ivanov", "petrova", "sidorov"]
    petrova = rows[1]
    assert petrova.calls == 3
    assert petrova.avg_score == 50.0  # (50+25+75)/3
    assert petrova.top_failures == (("need", 2), ("greet", 1), ("close", 1))
    assert rows[0].top_failures == ()
    assert stats.manager_ranking([], CHECKLIST) == []


def test_weekly_breaks_on_empty_weeks():
    calls = [
        _call(1, date(2026, 10, 5)),  # неделя с пн 5.10
        _call(2, date(2026, 10, 7), failed=("need",)),
        _call(3, date(2026, 10, 19)),  # через неделю — с пн 19.10
    ]
    points = stats.weekly(calls, _period(None, None))
    assert [p.week_start for p in points] == [date(2026, 10, 5), date(2026, 10, 12),
                                              date(2026, 10, 19)]
    assert [p.calls for p in points] == [2, 0, 1]
    assert points[0].avg_score == 75.0
    assert points[1].avg_score is None  # разрыв, не ноль
    assert points[2].avg_score == 100.0


def test_weekly_covers_period_bounds():
    points = stats.weekly([_call(1, date(2026, 10, 14))], _period(date(2026, 9, 28), TODAY))
    assert points[0].week_start == date(2026, 9, 28) and points[-1].week_start == date(2026, 10, 12)
    assert [p.calls for p in points] == [0, 0, 1]
    assert stats.weekly([], _period(None, None)) == []


def test_to_call_results_uses_effective_score():
    call = _call(1, date(2026, 10, 12), failed=("need",), manager="petrova")
    [result] = stats.to_call_results([call, _call(2, date(2026, 10, 12), status="queued")])
    assert result.meta.manager == "petrova" and result.meta.file_name == "1.mp3"
    assert result.score is call.score
