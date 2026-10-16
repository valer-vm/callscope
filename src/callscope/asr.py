"""Локальная расшифровка записей faster-whisper.

Стерео: каждый канал расшифровывается отдельно, сегменты сливаются по времени.
Совпадающие каналы или молчащий канал — запасной моно-путь: смена стороны на паузе
не короче порога или после вопроса. Записи никуда не отправляются; из сети один раз
скачивается только модель (если не задан локальный `model_path`).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal

import numpy as np

from callscope.errors import AudioError, CallScopeError
from callscope.model import Channel, RawTranscript, Segment

SAMPLE_RATE = 16000
LANGUAGE = "ru"
# Каналы считаются одинаковыми при корреляции выше порога.
_SAME_CHANNELS_CORRELATION = 0.98
# Канал «молчит»: средний уровень ниже −60 дБ или в 30+ раз тише другого.
_SILENCE_RMS = 1e-3
_SILENCE_RATIO = 1 / 30
# Стерео: пауза внутри канала, на которой сегмент режется (говорит другая сторона).
_STEREO_SPLIT_GAP = 0.5

Progress = Callable[[float], None]
_Word = tuple[float, float, str]  # начало, конец, текст (с ведущим пробелом, как у whisper)


def _rms(signal: np.ndarray) -> float:
    if signal.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(signal, dtype=np.float64))))


def _correlation(left: np.ndarray, right: np.ndarray) -> float:
    a = left.astype(np.float64) - float(np.mean(left))
    b = right.astype(np.float64) - float(np.mean(right))
    denominator = float(np.sqrt(np.dot(a, a) * np.dot(b, b)))
    return float(np.dot(a, b)) / denominator if denominator > 0 else 1.0


def _choose_mode(
    left: np.ndarray, right: np.ndarray
) -> tuple[Literal["stereo", "mono"], np.ndarray | None]:
    """«stereo» или «mono» + сигнал для моно-пути."""
    left_rms, right_rms = _rms(left), _rms(right)
    loud = max(left_rms, right_rms)
    quiet = min(left_rms, right_rms)
    if quiet < _SILENCE_RMS or quiet < loud * _SILENCE_RATIO:
        return "mono", left if left_rms >= right_rms else right
    if _correlation(left, right) > _SAME_CHANNELS_CORRELATION:
        return "mono", ((left + right) / 2).astype(np.float32)
    return "stereo", None


class Transcriber:
    """Обёртка над faster-whisper. Модель грузится при первой расшифровке."""

    def __init__(self, model_size: str, model_path: str | None = None) -> None:
        self.model_size = model_size
        self.model_path = model_path
        self._model: Any = None

    # --- модель --------------------------------------------------------------------

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        import faster_whisper

        if self.model_path:
            source = self.model_path
            notice = (
                f"Загружаю модель распознавания из {self.model_path} — без выхода в сеть, "
                "один раз за запуск; записи никуда не отправляются."
            )
        else:
            source = self.model_size
            notice = (
                f"Загружаю модель распознавания «{self.model_size}»: при первом запуске она "
                "скачивается из интернета один раз, сами записи никуда не отправляются."
            )
        print(notice, file=sys.stderr, flush=True)
        try:
            self._model = faster_whisper.WhisperModel(source, device="cpu", compute_type="int8")
        except Exception as exc:  # сеть, битая папка модели, неизвестный размер
            hint = (
                "проверьте путь model_path в callscope.yaml"
                if self.model_path
                else "при первом запуске нужен интернет; или укажите model_path в callscope.yaml"
            )
            raise CallScopeError(
                f"Не удалось загрузить модель распознавания «{source}» ({exc}); {hint}."
            ) from None
        return self._model

    def _run(self, audio: np.ndarray, **options: Any) -> list[Any]:
        model = self._load_model()
        segments, _info = model.transcribe(
            audio,
            language=LANGUAGE,
            vad_filter=True,
            condition_on_previous_text=False,
            **options,
        )
        return segments

    # --- расшифровка ----------------------------------------------------------------

    def transcribe(
        self,
        path: Path,
        mono_turn_gap: float,
        on_progress: Progress | None = None,
    ) -> RawTranscript:
        """Расшифровывает запись. Ошибки чтения и распознавания — AudioError."""
        left, right = self._decode(path)
        duration = len(left) / SAMPLE_RATE
        mode, mono_signal = _choose_mode(left, right)
        report: Progress = on_progress or (lambda _fraction: None)
        report(0.0)
        try:
            if mono_signal is None:
                segments = self._stereo(left, right, duration, report)
            else:
                segments = self._mono(mono_signal, duration, mono_turn_gap, report)
        except CallScopeError:
            raise
        except Exception as exc:
            raise AudioError(f"Не удалось распознать запись {path.name}: {exc}.") from None
        report(1.0)
        ordered = tuple(sorted(segments, key=lambda s: (s.start, s.channel)))
        return RawTranscript(segments=ordered, mode=mode, duration=duration)

    @staticmethod
    def _decode(path: Path) -> tuple[np.ndarray, np.ndarray]:
        if not path.is_file():
            raise AudioError(f"Файл {path.name} не найден.")
        try:
            left, right = _decode_stereo(path)
        except Exception:
            raise AudioError(
                f"Не удалось прочитать запись {path.name}: файл повреждён "
                "или формат не поддерживается."
            ) from None
        if len(left) == 0:
            raise AudioError(f"Запись {path.name} пустая — в ней нет звука.")
        return np.asarray(left, dtype=np.float32), np.asarray(right, dtype=np.float32)

    def _words(
        self, signal: np.ndarray, duration: float, report: Progress, number: int, total: int
    ) -> list[_Word]:
        words: list[_Word] = []
        for seg in self._run(signal, word_timestamps=True):
            for word in seg.words or [seg]:
                text = getattr(word, "word", None) or getattr(word, "text", "")
                if text.strip():
                    words.append((float(word.start), float(word.end), text))
            report(_fraction(number, seg.end, duration, total))
        return words

    def _stereo(
        self, left: np.ndarray, right: np.ndarray, duration: float, report: Progress
    ) -> list[Segment]:
        # Сегменты whisper бывают длиннее реплики (захватывают паузу, пока говорит другая
        # сторона), поэтому канал режется по паузам между словами.
        result: list[Segment] = []
        channels: tuple[tuple[Channel, np.ndarray], ...] = (("left", left), ("right", right))
        for number, (channel, signal) in enumerate(channels):
            words = self._words(signal, duration, report, number, total=2)
            for run in _runs(words, _STEREO_SPLIT_GAP, split_on_question=False):
                result.append(_segment(run, channel))
            report((number + 1) / 2)
        return result

    def _mono(
        self, signal: np.ndarray, duration: float, gap: float, report: Progress
    ) -> list[Segment]:
        return split_mono_turns(self._words(signal, duration, report, 0, total=1), gap)


def _decode_stereo(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """faster_whisper.decode_audio(split_stereo=True); моно-файл даёт два одинаковых канала."""
    from faster_whisper import decode_audio

    try:
        return decode_audio(str(path), sampling_rate=SAMPLE_RATE, split_stereo=True)
    except TypeError as exc:
        # faster-whisper 1.2 передаёт в av.open аргумент metadata_errors, которого нет
        # в PyAV ≥ 19. Тогда читаем тем же способом напрямую через PyAV.
        if "metadata_errors" not in str(exc):
            raise
    return _decode_stereo_av(path)


def _decode_stereo_av(path: Path) -> tuple[np.ndarray, np.ndarray]:
    import av

    resampler = av.audio.resampler.AudioResampler(format="s16", layout="stereo", rate=SAMPLE_RATE)
    chunks: list[np.ndarray] = []
    with av.open(str(path), mode="r") as container:
        for frame in container.decode(audio=0):
            for resampled in resampler.resample(frame):
                chunks.append(resampled.to_ndarray().reshape(-1))
        for resampled in resampler.resample(None):
            chunks.append(resampled.to_ndarray().reshape(-1))
    audio = np.concatenate(chunks).astype(np.float32) / 32768.0 if chunks else np.zeros(0)
    return audio[0::2], audio[1::2]


def _fraction(number: int, position: float, duration: float, total: int) -> float:
    inside = min(max(position / duration, 0.0), 1.0) if duration > 0 else 1.0
    return (number + inside) / total


def _runs(words: Iterable[_Word], gap: float, split_on_question: bool) -> list[list[_Word]]:
    """Режет слова на куски по паузе ≥ gap (и после «?», если split_on_question)."""
    runs: list[list[_Word]] = []
    for word in words:
        if runs:
            previous = runs[-1][-1]
            asked = split_on_question and previous[2].rstrip().endswith("?")
            if word[0] - previous[1] >= gap or asked:
                runs.append([word])
                continue
            runs[-1].append(word)
        else:
            runs.append([word])
    return runs


def _segment(run: list[_Word], channel: Channel) -> Segment:
    text = " ".join("".join(w[2] for w in run).split())
    return Segment(run[0][0], run[-1][1], text, channel)


def split_mono_turns(words: Iterable[_Word], gap: float) -> list[Segment]:
    """Делит слова моно-записи на реплики: смена стороны на паузе ≥ gap или после «?».

    Первый говорящий — условно «left».
    """
    sides: tuple[Channel, Channel] = ("left", "right")
    runs = _runs(words, gap, split_on_question=True)
    return [_segment(run, sides[i % 2]) for i, run in enumerate(runs)]
