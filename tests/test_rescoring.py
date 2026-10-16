"""Пересчёты без расшифровки: новый чек-лист, смена сторон, новый шаблон имени."""

from dataclasses import replace
from datetime import datetime

import pytest

from callscope import pipeline
from callscope.checklist import load_checklist
from callscope.config import load_config
from callscope.rescoring import reapply_template, rescore_all, rescore_call, swap_sides
from callscope.storage import Storage
from callscope.worker import Worker


@pytest.fixture
def storage(tmp_path) -> Storage:
    return Storage(tmp_path / "data")


def _settings(storage):
    return load_config(storage.config_path), load_checklist(storage.checklist_path)


def _done(storage, raw_from_fixture, normalizer, name, fixture):
    record, _ = storage.add_upload(name, f"{fixture}|{name}".encode())
    storage.next_queued()
    config, checklist = _settings(storage)
    raw = raw_from_fixture(fixture)
    storage.save_result(record.id, raw, pipeline.analyze_raw(name, raw, config, checklist,
                                                             normalizer))
    return record.id


def _drop_farewell(storage) -> None:
    """Рабочий чек-лист без пункта farewell (как после правки в настройках)."""
    text = storage.checklist_path.read_text(encoding="utf-8")
    start = text.index("  - id: farewell")
    end = text.find("\n  - id:", start + 1)
    end = text.find("\nsynonyms:", start) if end == -1 else end
    storage.checklist_path.write_text(text[:start] + text[end + 1:] if end != -1
                                      else text[:start], encoding="utf-8")
    assert "farewell" not in [i.id for i in load_checklist(storage.checklist_path).items]


def test_rescore_all_uses_new_checklist_keeps_corrections_without_transcriber(
    storage, raw_from_fixture, normalizer, fake_transcriber_cls
):
    call_id = _done(storage, raw_from_fixture, normalizer, "ivanov_2026-10-12_1530.m4a",
                    "no_need.json")
    storage.set_correction(call_id, "need", "passed", "")
    storage.set_correction(call_id, "farewell", "failed", "")
    _drop_farewell(storage)

    transcriber = fake_transcriber_cls()
    config, checklist = _settings(storage)
    worker = Worker(storage, lambda _config: transcriber, lambda: _settings(storage))
    worker.request_rescore()
    worker.run_once()

    assert transcriber.calls == []
    record = storage.get(call_id)
    assert [i.item_id for i in record.auto_items] == [i.id for i in checklist.items]
    assert set(record.corrections) == {"need"}
    # без farewell: need исправлен на passed, next_step провален → (5 + 20 + 15) / 65
    assert record.score.score == 61.5
    assert record.score.loss_item_id == "next_step"


def test_rescore_all_counts_and_reports_progress(storage, raw_from_fixture, normalizer):
    _done(storage, raw_from_fixture, normalizer, "a_2026-10-12_1530.m4a", "good.json")
    _done(storage, raw_from_fixture, normalizer, "b_2026-10-12_1530.m4a", "no_need.json")
    storage.add_upload("queued.mp3", b"q")
    config, checklist = _settings(storage)
    progress: list[tuple[int, int]] = []
    def record(done, total):
        progress.append((done, total))

    count = rescore_all(storage, config, checklist, normalizer, record)
    assert count == 2
    assert progress[-1] == (2, 2)


def test_rescore_keeps_meta_of_unmatched_after_template_change(
    storage, raw_from_fixture, normalizer
):
    call_id = _done(storage, raw_from_fixture, normalizer, "ivanov_2026-10-12_1530.m4a",
                    "good.json")
    config, checklist = _settings(storage)
    other = replace(config, filename_template="{date}_{manager}")
    rescore_call(storage, call_id, other, checklist, normalizer)
    assert storage.get(call_id).manager == "ivanov"


def test_swap_sides_reassigns_roles_from_raw_and_keeps_corrections(
    storage, raw_from_fixture, normalizer
):
    call_id = _done(storage, raw_from_fixture, normalizer, "ivanov_2026-10-12_1530.m4a",
                    "good.json")
    before = storage.get(call_id)
    storage.set_correction(call_id, "greeting", None, "комментарий")
    config, checklist = _settings(storage)

    swap_sides(storage, call_id, config, checklist, normalizer)
    after = storage.get(call_id)
    assert after.manager_channel == "right"
    assert after.dialogue.manager_channel == "right"
    assert [u.role for u in after.dialogue.utterances] == [
        "client" if u.role == "manager" else "manager" for u in before.dialogue.utterances
    ]
    assert after.score.score < before.score.score
    assert after.corrections["greeting"].comment == "комментарий"

    swap_sides(storage, call_id, config, checklist, normalizer)
    back = storage.get(call_id)
    assert back.manager_channel == "left" and back.score.score == before.score.score


def test_reapply_template_updates_matched_and_keeps_others(storage, raw_from_fixture,
                                                         normalizer):
    matched = _done(storage, raw_from_fixture, normalizer, "2026-10-12_1530_petrova.m4a",
                    "good.json")
    unmatched = _done(storage, raw_from_fixture, normalizer, "ivanov_2026-10-12_1530.m4a",
                      "good.json")
    config, _ = _settings(storage)
    before_unmatched = storage.get(unmatched)
    new_config = replace(config, filename_template="{date}_{time}_{manager}")

    assert reapply_template(storage, new_config) == (1, 1)
    got = storage.get(matched)
    assert got.manager == "petrova" and got.matched_template
    assert got.call_datetime == datetime(2026, 10, 12, 15, 30)
    kept = storage.get(unmatched)
    assert (kept.manager, kept.call_datetime, kept.matched_template) == (
        before_unmatched.manager, before_unmatched.call_datetime, True
    )


def _set_weight(storage, item_id: str, weight: int) -> None:
    text = storage.checklist_path.read_text(encoding="utf-8")
    start = text.index(f"  - id: {item_id}")
    at = text.index("    weight:", start)
    end = text.index("\n", at)
    storage.checklist_path.write_text(
        text[:at] + f"    weight: {weight}" + text[end:], encoding="utf-8"
    )


def test_correction_uses_weights_of_auto_score_until_rescore(
    storage, raw_from_fixture, normalizer
):
    call_id = _done(storage, raw_from_fixture, normalizer, "ivanov_2026-10-12_1530.m4a",
                    "no_need.json")
    _set_weight(storage, "greeting", 50)  # вес изменили в настройках, переоценки ещё не было

    corrected = storage.set_correction(call_id, "need", "passed", "")
    # по старым весам: (5 + 20 + 15 + 5) / (5 + 20 + 15 + 25 + 5) = 64.3
    assert corrected.score.score == 64.3

    config, checklist = _settings(storage)
    rescore_all(storage, config, checklist, normalizer)
    # по новым весам: (50 + 20 + 15 + 5) / (50 + 20 + 15 + 25 + 5) = 78.3
    assert storage.get(call_id).score.score == 78.3
