"""Деление моно по голосу: синтетические «голоса» — гармоники с разной высотой и тембром."""

import numpy as np

from callscope import diarize
from callscope.asr import split_mono_turns

RATE = diarize.SAMPLE_RATE


def voice(seconds: float, pitch: float, brightness: float, seed: int) -> np.ndarray:
    """Гармонический сигнал: высота pitch, спад гармоник задаёт тембр; лёгкий шум."""
    t = np.arange(int(seconds * RATE)) / RATE
    signal = np.zeros_like(t)
    for k in range(1, 30):
        if k * pitch > 3800:
            break
        signal += np.sin(2 * np.pi * k * pitch * t) / k**brightness
    noise = np.random.default_rng(seed).normal(0, 0.01, len(t))
    return (0.2 * signal / np.abs(signal).max() + noise).astype(np.float32)


LOW = {"pitch": 110.0, "brightness": 1.6}  # низкий, глухой
HIGH = {"pitch": 230.0, "brightness": 0.8}  # высокий, яркий


def build(turns: list[tuple[dict, float, str]], pause: float) -> tuple[np.ndarray, list]:
    """Сигнал и слова whisper для последовательности реплик (голос, длительность, текст)."""
    pieces, words, now = [], [], 0.0
    for number, (who, seconds, text) in enumerate(turns):
        pieces.append(voice(seconds, seed=number, **who))
        words.append((now, now + seconds, f" {text}"))
        now += seconds
        pieces.append(np.zeros(int(pause * RATE), dtype=np.float32))
        now += pause
    return np.concatenate(pieces), words


def test_quick_turns_are_split_by_voice():
    # Паузы 0,3 с — короче порога 0,8 с: старое правило склеило бы всё в одну реплику.
    signal, words = build(
        [
            (LOW, 1.2, "Здравствуйте, компания СтройДом"),
            (HIGH, 0.8, "Добрый день"),
            (LOW, 1.0, "Чем могу помочь"),
            (HIGH, 1.4, "Нужен ламинат на кухню"),
            (HIGH, 0.9, "метров двенадцать"),
            (LOW, 1.1, "Отлично, подберём"),
        ],
        pause=0.3,
    )
    segments = split_mono_turns(words, gap=0.8, signal=signal)
    assert [(s.channel, s.text) for s in segments] == [
        ("left", "Здравствуйте, компания СтройДом"),
        ("right", "Добрый день"),
        ("left", "Чем могу помочь"),
        ("right", "Нужен ламинат на кухню метров двенадцать"),
        ("left", "Отлично, подберём"),
    ]


def test_first_speaker_is_left_whoever_it_is():
    signal, words = build(
        [(HIGH, 1.0, "Алло"), (LOW, 1.0, "Здравствуйте"), (HIGH, 1.0, "Да"), (LOW, 1.0, "Хорошо")],
        pause=0.3,
    )
    channels = [s.channel for s in split_mono_turns(words, gap=0.8, signal=signal)]
    assert channels == ["left", "right", "left", "right"]


def test_one_voice_falls_back_to_pause_rule():
    # Один и тот же голос — по голосу не делим; старое правило: пауза ≥ 0,8 с меняет сторону.
    signal, words = build(
        [(LOW, 1.0, "Раз"), (LOW, 1.0, "два"), (LOW, 1.0, "три"), (LOW, 1.0, "четыре")],
        pause=0.3,
    )
    units = diarize.split_units(words)
    assert diarize.assign_speakers(signal, units) is None
    segments = split_mono_turns(words, gap=0.8, signal=signal)
    assert [(s.channel, s.text) for s in segments] == [("left", "Раз два три четыре")]


def test_too_few_pieces_fall_back():
    signal, words = build([(LOW, 1.0, "Алло."), (HIGH, 1.0, "Да.")], pause=0.3)
    assert diarize.assign_speakers(signal, diarize.split_units(words)) is None


def test_units_break_on_pause_and_sentence_end():
    words = [
        (0.0, 0.4, " Добрый"),
        (0.45, 0.8, " день."),  # конец предложения
        (0.85, 1.2, " Слушаю"),
        (1.6, 2.0, " вас"),  # пауза 0,4 ≥ 0,25
    ]
    assert [[w[2] for w in unit] for unit in diarize.split_units(words)] == [
        [" Добрый", " день."],
        [" Слушаю"],
        [" вас"],
    ]
