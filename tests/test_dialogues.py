"""Тестовые диалоги из scripts/dialogues.yaml: оценка по чек-листу по умолчанию, без аудио."""

import importlib.util
from pathlib import Path

import pytest

from callscope.checklist import load_checklist
from callscope.config import Thresholds
from callscope.model import Dialogue
from callscope.scoring import score_call

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "generate_examples", ROOT / "scripts/generate_examples.py"
)
generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generator)

CALLS = generator.load_calls()
IDS = [call["file"] for call in CALLS]


@pytest.fixture(scope="module")
def checklist():
    return load_checklist(ROOT / "checklist.yaml")


def _score(call, checklist, normalizer):
    dialogue = Dialogue.from_json(generator.build_dialogue_json(call))
    return score_call(dialogue, checklist, Thresholds(), normalizer)


def test_five_calls_three_managers_with_template_names():
    assert len(CALLS) == 5
    managers = {call["file"].split("_")[0] for call in CALLS}
    assert len(managers) == 3
    for call in CALLS:
        manager, date, time = call["file"].split("_")
        assert manager.isalpha() and manager.isascii()
        assert len(date) == 10 and len(time) == 4


def test_dialogues_alternate_and_start_with_manager():
    for call in CALLS:
        roles = [role for role, _ in generator.call_lines(call)]
        assert roles[0] == "manager"
        assert all(a != b for a, b in zip(roles, roles[1:], strict=False))


@pytest.mark.parametrize("call", CALLS, ids=IDS)
def test_expected_grade_and_loss_item(call, checklist, normalizer):
    result = _score(call, checklist, normalizer)
    expect = call["expect"]
    if expect["grade"] == "good":
        assert result.score >= 80
        assert result.grade == "good"
    elif expect["grade"] == "bad":
        assert result.score < 50
    else:
        assert result.grade != "good"
    assert result.loss_item_id == expect["loss"]


def test_good_calls_pass_every_applicable_item(checklist, normalizer):
    good = [c for c in CALLS if c["expect"]["grade"] == "good"]
    assert len(good) == 2
    for call in good:
        statuses = {r.item_id: r.status for r in _score(call, checklist, normalizer).items}
        assert "failed" not in statuses.values()


def test_objection_calls_cover_handled_failed_and_absent(checklist, normalizer):
    status = {
        call["file"]: {r.item_id: r.status for r in _score(call, checklist, normalizer).items}[
            "objection"
        ]
        for call in CALLS
    }
    assert status["ivanov_2026-10-12_1530"] == "passed"
    assert status["sidorov_2026-10-14_1400"] == "failed"
    assert status["petrova_2026-10-16_0930"] == "na"
