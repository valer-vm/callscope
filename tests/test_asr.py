"""Расшифровка: настоящий decode_audio на WAV из tmp_path, модель — подставная.

Медленный тест с настоящей моделью и голосом `say` запускается только с CALLSCOPE_SLOW=1.
"""

import math
import os
import shutil
import struct
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import faster_whisper
import numpy as np
import pytest

from callscope.asr import Transcriber
from callscope.errors import AudioError

RATE = 16000


def write_wav(path: Path, left: np.ndarray, right: np.ndarray | None = None) -> Path:
    channels = [left] if right is None else [left, right]
    frames = np.stack(channels, axis=1)
    pcm = (np.clip(frames, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(len(channels))
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm.tobytes())
    return path


def tone(seconds: float, spans: list[tuple[float, float]], freq: float = 440.0) -> np.ndarray:
    """Синус в указанных интервалах, тишина вне их."""
    signal = np.zeros(int(seconds * RATE), dtype=np.float32)
    for start, end in spans:
        idx = np.arange(int(start * RATE), int(end * RATE))
        signal[idx] = 0.3 * np.sin(2 * math.pi * freq * idx / RATE)
    return signal


@dataclass
class FakeWord:
    start: float
    end: float
    word: str
    probability: float = 1.0


@dataclass
class FakeSegment:
    start: float
    end: float
    text: str
    words: list | None


@dataclass
class FakeInfo:
    duration: float


class FakeModel:
    """«Распознаёт» звучащие куски сигнала как слова; либо отдаёт заданные слова."""

    instances: list["FakeModel"] = []
    script: list[tuple[float, float, str]] | None = None

    def __init__(self, model_size_or_path, **kwargs):
        self.model_size_or_path = model_size_or_path
        self.kwargs = kwargs
        self.calls = 0
        FakeModel.instances.append(self)

    def transcribe(self, audio, **kwargs):
        self.calls += 1
        if FakeModel.script is not None:
            words = [FakeWord(s, e, f" {t}") for s, e, t in FakeModel.script]
        else:
            loud = np.abs(audio) > 0.05
            words, start = [], None
            step = RATE // 100
            for i in range(0, len(audio), step):
                on = bool(loud[i : i + step].any())
                if on and start is None:
                    start = i / RATE
                if not on and start is not None:
                    words.append(FakeWord(start, i / RATE, " звук"))
                    start = None
            if start is not None:
                words.append(FakeWord(start, len(audio) / RATE, " звук"))
        segments = [
            FakeSegment(w.start, w.end, w.word, [w] if kwargs.get("word_timestamps") else None)
            for w in words
        ]
        return iter(segments), FakeInfo(duration=len(audio) / RATE)


@pytest.fixture
def fake_model(monkeypatch):
    FakeModel.instances = []
    FakeModel.script = None
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    return FakeModel


def test_stereo_channels_transcribed_separately_and_merged_by_time(tmp_path, fake_model):
    left = tone(4.0, [(0.0, 1.0), (2.5, 3.0)])
    right = tone(4.0, [(1.3, 2.2)], freq=300.0)
    path = write_wav(tmp_path / "call.wav", left, right)
    raw = Transcriber("small").transcribe(path, mono_turn_gap=0.8)
    assert raw.mode == "stereo"
    assert [s.channel for s in raw.segments] == ["left", "right", "left"]
    assert raw.segments[1].start == pytest.approx(1.3, abs=0.05)
    assert raw.duration == pytest.approx(4.0, abs=0.01)


def test_identical_channels_take_mono_path(tmp_path, fake_model):
    signal = tone(3.0, [(0.0, 0.8), (2.0, 2.6)])
    path = write_wav(tmp_path / "same.wav", signal, signal.copy())
    raw = Transcriber("small").transcribe(path, mono_turn_gap=0.8)
    assert raw.mode == "mono"
    # пауза 1,2 с ≥ 0,8 → смена стороны
    assert [s.channel for s in raw.segments] == ["left", "right"]


def test_silent_channel_takes_mono_path(tmp_path, fake_model):
    left = tone(3.0, [(0.0, 0.8), (2.0, 2.6)])
    path = write_wav(tmp_path / "half.wav", left, np.zeros_like(left))
    raw = Transcriber("small").transcribe(path, mono_turn_gap=0.8)
    assert raw.mode == "mono"
    assert len(raw.segments) == 2


def test_mono_file_splits_on_pause_and_question(tmp_path, fake_model):
    path = write_wav(tmp_path / "mono.wav", tone(6.0, [(0.0, 5.0)]))
    fake_model.script = [
        (0.0, 0.5, "Здравствуйте,"),
        (0.6, 1.0, "СтройДом."),  # пауза 0,1 — та же сторона
        (1.1, 1.6, "Удобно говорить?"),  # вопрос — дальше другая сторона
        (1.7, 2.0, "Да."),
        (3.0, 3.5, "Отлично."),  # пауза 1,0 ≥ 0,8 — снова менеджер
    ]
    raw = Transcriber("small").transcribe(path, mono_turn_gap=0.8)
    assert raw.mode == "mono"
    assert [(s.channel, s.text) for s in raw.segments] == [
        ("left", "Здравствуйте, СтройДом. Удобно говорить?"),
        ("right", "Да."),
        ("left", "Отлично."),
    ]


def test_mono_gap_threshold_is_configurable(tmp_path, fake_model):
    path = write_wav(tmp_path / "mono.wav", tone(3.0, [(0.0, 2.0)]))
    fake_model.script = [(0.0, 0.5, "Раз"), (1.0, 1.5, "два")]  # пауза 0,5
    assert len(Transcriber("small").transcribe(path, mono_turn_gap=0.8).segments) == 1
    assert len(Transcriber("small").transcribe(path, mono_turn_gap=0.4).segments) == 2


def test_model_is_lazy_loaded_once_with_one_notice(tmp_path, fake_model, capsys):
    path = write_wav(tmp_path / "mono.wav", tone(1.0, [(0.0, 0.5)]))
    transcriber = Transcriber("small")
    assert fake_model.instances == []
    transcriber.transcribe(path, mono_turn_gap=0.8)
    transcriber.transcribe(path, mono_turn_gap=0.8)
    assert len(fake_model.instances) == 1
    out = capsys.readouterr()
    lines = [line for line in (out.out + out.err).splitlines() if line.strip()]
    assert len(lines) == 1
    assert "один раз" in lines[0] and "не отправляются" in lines[0]


def test_model_path_used_instead_of_download(tmp_path, fake_model):
    path = write_wav(tmp_path / "mono.wav", tone(1.0, [(0.0, 0.5)]))
    Transcriber("small", model_path=str(tmp_path / "models" / "small")).transcribe(path, 0.8)
    assert fake_model.instances[0].model_size_or_path == str(tmp_path / "models" / "small")


def test_progress_reaches_one(tmp_path, fake_model):
    left = tone(2.0, [(0.0, 0.5)])
    right = tone(2.0, [(1.0, 1.5)], freq=300.0)
    path = write_wav(tmp_path / "call.wav", left, right)
    seen: list[float] = []
    Transcriber("small").transcribe(path, 0.8, on_progress=seen.append)
    assert seen and seen[-1] == pytest.approx(1.0)
    assert seen == sorted(seen) and all(0 <= p <= 1 for p in seen)


def test_unreadable_file_is_audio_error_without_loading_model(tmp_path, fake_model):
    path = tmp_path / "broken.mp3"
    path.write_bytes(b"\x00\x01 not audio at all " * 50)
    with pytest.raises(AudioError) as info:
        Transcriber("small").transcribe(path, 0.8)
    assert "broken.mp3" in info.value.message_ru
    assert fake_model.instances == []


def test_missing_file_is_audio_error(tmp_path, fake_model):
    with pytest.raises(AudioError):
        Transcriber("small").transcribe(tmp_path / "nope.wav", 0.8)


# --- медленный тест: настоящая модель small и голос Milena ---------------------------


def _model_available(size: str) -> bool:
    try:
        from faster_whisper.utils import download_model

        download_model(size, local_files_only=True)
        return True
    except Exception:
        return False


def _say_wav(text: str, folder: Path, name: str) -> np.ndarray:
    aiff, wav = folder / f"{name}.aiff", folder / f"{name}.wav"
    subprocess.run(["say", "-v", "Milena", "-o", str(aiff), text], check=True)
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
        check=True,
    )
    with wave.open(str(wav), "rb") as src:
        frames = src.readframes(src.getnframes())
    count = len(frames) // 2
    return np.array(struct.unpack(f"<{count}h", frames), dtype=np.float32) / 32768


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("CALLSCOPE_SLOW") != "1", reason="медленный: CALLSCOPE_SLOW=1")
@pytest.mark.skipif(shutil.which("say") is None, reason="нужен macOS say")
def test_real_model_on_say_stereo_call(tmp_path):
    if not _model_available("small"):
        pytest.skip("модель small не скачана")
    lines = [
        ("left", "Здравствуйте! Меня зовут Анна, компания СтройДом. Чем могу помочь?"),
        ("right", "Добрый день. Мне нужен ламинат на кухню."),
        ("left", "Какая площадь кухни?"),
        ("right", "Примерно двенадцать метров."),
    ]
    pause = np.zeros(int(0.6 * RATE), dtype=np.float32)
    left_parts, right_parts = [], []
    for i, (side, text) in enumerate(lines):
        voice = _say_wav(text, tmp_path, f"l{i}")
        silence = np.zeros_like(voice)
        left_parts += [voice if side == "left" else silence, pause]
        right_parts += [voice if side == "right" else silence, pause]
    left, right = np.concatenate(left_parts), np.concatenate(right_parts)
    stereo = write_wav(tmp_path / "stereo.wav", left, right)
    mono = write_wav(tmp_path / "mono.wav", left + right)

    transcriber = Transcriber("small")
    started = time.monotonic()
    raw = transcriber.transcribe(stereo, mono_turn_gap=0.8)
    elapsed = time.monotonic() - started
    print(f"\nstereo {raw.duration:.1f} с расшифровано за {elapsed:.1f} с")
    assert raw.mode == "stereo"
    assert [s.channel for s in raw.segments][:4] == ["left", "right", "left", "right"]
    assert "стройдом" in raw.segments[0].text.lower().replace(" ", "")

    mono_raw = transcriber.transcribe(mono, mono_turn_gap=0.5)
    assert mono_raw.mode == "mono"
    assert len({s.channel for s in mono_raw.segments}) == 2

    same = write_wav(tmp_path / "same.wav", left + right, left + right)
    assert transcriber.transcribe(same, mono_turn_gap=0.5).mode == "mono"
