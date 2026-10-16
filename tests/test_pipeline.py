"""Общий конвейер: расшифровка с кэшем и оценка сырой расшифровки (без модели и аудио)."""

from pathlib import Path

from callscope import pipeline
from callscope.cache import TranscriptCache
from callscope.checklist import load_checklist
from callscope.config import DEFAULTS_DIR, load_config
from callscope.scoring import score_call


def _settings():
    config = load_config(DEFAULTS_DIR / "callscope.yaml")
    checklist = load_checklist(DEFAULTS_DIR / "checklist.yaml")
    return config, checklist


def _audio(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_transcript_for_uses_cache_on_second_call(tmp_path, fake_transcriber_cls):
    config, _ = _settings()
    transcriber = fake_transcriber_cls()
    cache = TranscriptCache(tmp_path / "cache")
    audio = _audio(tmp_path, "a.mp3", "good.json")
    progress: list[float] = []

    raw1, cached1 = pipeline.transcript_for(audio, transcriber, cache, config, progress.append)
    raw2, cached2 = pipeline.transcript_for(audio, transcriber, cache, config)

    assert (cached1, cached2) == (False, True)
    assert raw1 == raw2
    assert transcriber.calls == ["a.mp3"]
    assert progress == [0.5]


def test_transcript_for_without_cache_always_transcribes(tmp_path, fake_transcriber_cls):
    config, _ = _settings()
    transcriber = fake_transcriber_cls()
    audio = _audio(tmp_path, "a.mp3", "good.json")
    pipeline.transcript_for(audio, transcriber, None, config)
    _raw, cached = pipeline.transcript_for(audio, transcriber, None, config)
    assert cached is False
    assert transcriber.calls == ["a.mp3", "a.mp3"]


def test_analyze_raw_matches_stage1_scoring(
    raw_from_fixture, load_dialogue, normalizer
):
    config, checklist = _settings()
    for name in ("good.json", "failed_objection.json", "no_need.json", "no_objection.json"):
        expected = score_call(load_dialogue(name), checklist, config.thresholds, normalizer)
        result = pipeline.analyze_raw(
            "ivanov_2026-10-12_1530.m4a", raw_from_fixture(name), config, checklist, normalizer
        )
        assert result.score.score == expected.score, name
        assert result.meta.manager == "ivanov"
        assert result.dialogue.manager_channel == "left"


def test_analyze_raw_manual_channel_swaps_roles(raw_from_fixture, normalizer):
    config, checklist = _settings()
    raw = raw_from_fixture("good.json")
    auto = pipeline.analyze_raw("x.mp3", raw, config, checklist, normalizer)
    swapped = pipeline.analyze_raw(
        "x.mp3", raw, config, checklist, normalizer, manager_channel="right"
    )
    assert swapped.dialogue.manager_channel == "right"
    assert swapped.dialogue.role_source == "manual"
    assert [u.role for u in swapped.dialogue.utterances] == [
        "client" if u.role == "manager" else "manager" for u in auto.dialogue.utterances
    ]
    assert swapped.score.score < auto.score.score
    assert any("не подходит под шаблон" in w for w in auto.warnings)
