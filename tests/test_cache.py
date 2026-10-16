import os

import pytest

from callscope.cache import TranscriptCache
from callscope.errors import AudioError
from callscope.model import RawTranscript, Segment

RAW = RawTranscript(
    segments=(
        Segment(0.0, 1.5, "Здравствуйте, компания СтройДом.", "left"),
        Segment(2.0, 3.0, "Добрый день.", "right"),
    ),
    mode="stereo",
    duration=3.0,
)


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "call.wav"
    path.write_bytes(b"RIFF-audio-bytes-1")
    return path


@pytest.fixture
def cache(tmp_path):
    return TranscriptCache(tmp_path / ".callscope-cache")


def test_put_then_get_returns_same_transcript(cache, audio):
    key = cache.key(audio, "small", 0.8)
    assert cache.get(key) is None
    cache.put(key, RAW)
    assert cache.get(key) == RAW


def test_key_changes_with_content_and_model_not_with_name(cache, audio, tmp_path):
    key = cache.key(audio, "small", 0.8)
    assert cache.key(audio, "small", 0.8) == key
    assert cache.key(audio, "medium", 0.8) != key
    copy = tmp_path / "renamed.wav"
    copy.write_bytes(audio.read_bytes())
    assert cache.key(copy, "small", 0.8) == key
    audio.write_bytes(b"RIFF-audio-bytes-2")
    assert cache.key(audio, "small", 0.8) != key


def test_key_does_not_depend_on_checklist(cache, audio, tmp_path):
    key = cache.key(audio, "small", 0.8)
    (tmp_path / "checklist.yaml").write_text("items: []", encoding="utf-8")
    assert cache.key(audio, "small", 0.8) == key


def test_broken_json_is_a_miss(cache, audio):
    key = cache.key(audio, "small", 0.8)
    cache.put(key, RAW)
    for path in cache.directory.iterdir():
        path.write_text("{ не json", encoding="utf-8")
    assert cache.get(key) is None


def test_wrong_shape_json_is_a_miss(cache, audio):
    key = cache.key(audio, "small", 0.8)
    cache.put(key, RAW)
    for path in cache.directory.iterdir():
        path.write_text('{"segments": 5}', encoding="utf-8")
    assert cache.get(key) is None


def test_put_is_atomic_via_replace(cache, audio, monkeypatch):
    key = cache.key(audio, "small", 0.8)
    cache.put(key, RAW)
    calls = []
    real_replace = os.replace

    def failing_replace(src, dst):
        calls.append((src, dst))
        raise KeyboardInterrupt  # прерванный запуск в момент записи

    monkeypatch.setattr(os, "replace", failing_replace)
    other = RawTranscript(segments=(), mode="mono", duration=1.0)
    with pytest.raises(KeyboardInterrupt):
        cache.put(key, other)
    monkeypatch.setattr(os, "replace", real_replace)
    assert calls, "запись должна идти через временный файл и os.replace"
    assert cache.get(key) == RAW  # старая запись не испорчена
    assert [p.suffix for p in cache.directory.iterdir()] == [".json"]  # tmp убран


def test_key_of_missing_file_is_audio_error(cache, tmp_path):
    with pytest.raises(AudioError):
        cache.key(tmp_path / "nope.wav", "small", 0.8)


def test_key_changes_with_mono_turn_gap(cache, audio):
    assert cache.key(audio, "small", 0.8) != cache.key(audio, "small", 1.2)
    assert cache.key(audio, "small", 0.8) == cache.key(audio, "small", 0.8)
