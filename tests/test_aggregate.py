from datetime import datetime

from callscope.aggregate import build_summary
from callscope.checklist import Checklist, ChecklistItem
from callscope.config import Thresholds
from callscope.model import (
    CallMeta,
    CallResult,
    CallScore,
    Dialogue,
    ItemResult,
    SkippedFile,
)

CHECKLIST = Checklist(
    items=(
        ChecklistItem(id="greeting", title="Приветствие", weight=5),
        ChecklistItem(id="need", title="Выявление потребности", weight=20),
        ChecklistItem(id="next_step", title="Следующий шаг", weight=25),
    )
)
EMPTY_DIALOGUE = Dialogue((), False, "left", "manual")


def make_call(manager: str, score: float, failed: tuple[str, ...]) -> CallResult:
    items = tuple(
        ItemResult(i.id, "failed" if i.id in failed else "passed", "причина", ())
        for i in CHECKLIST.items
    )
    loss = next((i.id for i in CHECKLIST.items if i.id in failed), None)
    return CallResult(
        meta=CallMeta(f"{manager}.wav", manager, None, True),
        dialogue=EMPTY_DIALOGUE,
        score=CallScore(items, score, loss, "good"),
        warnings=(),
    )


def build(calls, skipped=()):
    return build_summary(
        calls, list(skipped), CHECKLIST, Thresholds(), "calls",
        generated_at=datetime(2026, 10, 16, 12, 0),
    )


CALLS = [
    make_call("ivanov", 100, ()),
    make_call("ivanov", 40, ("need", "next_step")),
    make_call("petrov", 20, ("need",)),
    make_call("petrov", 60, ("next_step",)),
    make_call("sidorov", 80, ()),
]


def test_manager_averages_sorted_descending():
    s = build(CALLS)
    assert [(m.manager, m.calls, m.avg_score) for m in s.managers] == [
        ("sidorov", 1, 80.0),
        ("ivanov", 2, 70.0),
        ("petrov", 2, 40.0),
    ]
    assert s.avg_score == 60.0


def test_top_failures_per_manager():
    s = build(CALLS)
    ivanov = next(m for m in s.managers if m.manager == "ivanov")
    assert ivanov.top_failures == (("need", 1), ("next_step", 1))
    sidorov = next(m for m in s.managers if m.manager == "sidorov")
    assert sidorov.top_failures == ()


def test_top_failures_capped_at_three():
    checklist = Checklist(
        items=tuple(ChecklistItem(id=f"p{n}", title=f"П{n}", weight=1) for n in range(5))
    )
    items = tuple(ItemResult(f"p{n}", "failed", "x", ()) for n in range(5))
    call = CallResult(
        CallMeta("a.wav", "a", None, True), EMPTY_DIALOGUE, CallScore(items, 0, "p0", "bad"), ()
    )
    s = build_summary([call], [], checklist, Thresholds(), "d")
    assert len(s.managers[0].top_failures) == 3


def test_loss_distribution_in_checklist_order_and_ranking():
    s = build(CALLS)
    assert s.loss_distribution == (("greeting", 0), ("need", 2), ("next_step", 1))
    assert s.failure_ranking == (("need", 2), ("next_step", 2))


def test_conclusion_names_stage_and_share():
    s = build(CALLS)
    assert s.conclusion == "Чаще всего звонки теряются на этапе «Выявление потребности» — 2 из 5."


def test_conclusion_without_failures():
    s = build([make_call("ivanov", 100, ())])
    assert s.conclusion.startswith("Потерь не найдено")
    assert all(n == 0 for _, n in s.loss_distribution)


def test_empty_calls_do_not_crash():
    s = build([], [SkippedFile("x.mp3", "не читается")])
    assert s.calls == () and s.managers == ()
    assert s.avg_score == 0.0
    assert s.skipped[0].file_name == "x.mp3"
    assert s.conclusion
