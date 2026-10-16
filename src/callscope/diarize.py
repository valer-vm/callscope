"""Деление моно-записи на две стороны по голосу — без нейросетей и внешних моделей.

Слова режутся на короткие куски (пауза от 0,25 с или конец предложения). Для каждого
куска считается «отпечаток голоса»: форма спектральной огибающей (24 полосы 100–3800 Гц,
без учёта громкости) и высота тона. Куски делятся на две группы взвешенным k-means,
соседние куски одной группы склеиваются в реплики.

Если кусков слишком мало, почти вся речь попала в одну группу или «голоса» групп почти
не различаются (один и тот же голос), разделить по голосу не удалось — вызывающий код
берёт запасное правило по паузам.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

SAMPLE_RATE = 16000
# Кусок речи заканчивается на такой паузе или на конце предложения.
UNIT_GAP = 0.25
_MIN_UNITS = 4
# Доля речи меньшей группы: ниже — считаем, что второй голос не найден.
_MIN_SHARE = 0.1
# Среднее по полосам различие огибающих двух групп (лог энергии, ≈ 4,3 дБ на единицу).
# На тестовых звонках один голос даёт 0,7–1,3, разные голоса — от 1,2.
_MIN_ENVELOPE_GAP = 1.0

_FRAME = 512
_HOP = 160
_EDGES = np.geomspace(100.0, 3800.0, 25)
_FREQS = np.fft.rfftfreq(_FRAME, 1 / SAMPLE_RATE)
_BANDS = [
    np.where((_FREQS >= lo) & (_FREQS < hi))[0]
    for lo, hi in zip(_EDGES[:-1], _EDGES[1:], strict=True)
]
_WINDOW = np.hanning(_FRAME)
# Высота тона ищется в 70–400 Гц; кадр считается озвученным при автокорреляции ≥ 0,35.
_PITCH_LAG = (SAMPLE_RATE // 400, SAMPLE_RATE // 70)
_VOICED = 0.35
_RESTARTS = 20

Word = tuple[float, float, str]  # начало, конец, текст


def split_units(words: Sequence[Word], gap: float = UNIT_GAP) -> list[list[Word]]:
    """Короткие куски речи: пауза ≥ gap или слово на «.», «?», «!» закрывает кусок."""
    units: list[list[Word]] = []
    for word in words:
        if units:
            previous = units[-1][-1]
            sentence_end = previous[2].rstrip()[-1:] in (".", "?", "!")
            if word[0] - previous[1] < gap and not sentence_end:
                units[-1].append(word)
                continue
        units.append([word])
    return units


def assign_speakers(signal: np.ndarray, units: Sequence[Sequence[Word]]) -> list[int] | None:
    """Номер стороны (0 — кто заговорил первым, 1 — другой) для каждого куска.

    None — по голосу разделить не удалось.
    """
    if len(units) < _MIN_UNITS:
        return None
    features = [_voice_print(signal, unit) for unit in units]
    known = [i for i, f in enumerate(features) if f is not None]
    if len(known) < _MIN_UNITS:
        return None
    matrix = np.array([features[i] for i in known])
    column_means = np.nanmean(matrix, axis=0)
    raw = np.where(np.isnan(matrix), np.nan_to_num(column_means), matrix)
    matrix = (raw - raw.mean(axis=0)) / (raw.std(axis=0) + 1e-9)
    weights = np.array([units[i][-1][1] - units[i][0][0] for i in known]) + 0.05

    labels = _two_means(matrix, weights)
    share = float(weights[labels == 0].sum() / weights.sum())
    if min(share, 1 - share) < _MIN_SHARE:
        return None
    envelopes = [
        np.average(raw[labels == k, :-1], axis=0, weights=weights[labels == k]) for k in (0, 1)
    ]
    gap = float(np.linalg.norm(envelopes[0] - envelopes[1]) / np.sqrt(len(envelopes[0])))
    if gap < _MIN_ENVELOPE_GAP:
        return None

    result: list[int] = [0] * len(units)
    by_index = dict(zip(known, labels.tolist(), strict=True))
    previous = 0
    for i in range(len(units)):
        # Кусок без отпечатка (слишком короткий) — той же стороны, что предыдущий.
        previous = by_index.get(i, previous)
        result[i] = previous
    first = result[0]
    return [label if first == 0 else 1 - label for label in result]


def _frames(chunk: np.ndarray) -> np.ndarray:
    if len(chunk) < _FRAME:
        return np.zeros((0, _FRAME))
    count = 1 + (len(chunk) - _FRAME) // _HOP
    index = np.arange(_FRAME)[None, :] + _HOP * np.arange(count)[:, None]
    return chunk[index] * _WINDOW


def _pitch(frames: np.ndarray) -> float:
    low, high = _PITCH_LAG
    found: list[float] = []
    for frame in frames:
        correlation = np.correlate(frame, frame, "full")[_FRAME - 1 :]
        if correlation[0] <= 0:
            continue
        window = correlation[low:high]
        peak = int(np.argmax(window))
        if window[peak] / correlation[0] >= _VOICED:
            found.append(SAMPLE_RATE / (low + peak))
    return float(np.median(found)) if found else float("nan")


def _voice_print(signal: np.ndarray, unit: Sequence[Word]) -> np.ndarray | None:
    start, end = int(unit[0][0] * SAMPLE_RATE), int(unit[-1][1] * SAMPLE_RATE)
    frames = _frames(np.asarray(signal[start:end], dtype=np.float64))
    if len(frames) == 0:
        return None
    spectrum = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    energy = spectrum.sum(axis=1)
    loud = energy > np.percentile(energy, 40)
    if not loud.any():
        return None
    bands = np.log(np.stack([spectrum[loud][:, b].sum(axis=1) for b in _BANDS], axis=1) + 1e-9)
    envelope = bands.mean(axis=0)
    envelope -= envelope.mean()  # форма огибающей, не громкость
    pitch = _pitch(frames[loud][::3])
    # Логарифм высоты с весом 8 — сопоставим с разбросом полос огибающей.
    pitch_feature = np.log(pitch) * 8 if pitch == pitch else np.nan
    return np.concatenate([envelope, [pitch_feature]])


def _two_means(matrix: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Взвешенный k-means на две группы с перезапусками (k-means++), лучший по инерции."""
    rng = np.random.default_rng(0)
    probabilities = weights / weights.sum()
    best_labels, best_inertia = np.zeros(len(matrix), dtype=int), np.inf
    for _ in range(_RESTARTS):
        first = rng.choice(len(matrix), p=probabilities)
        distance = ((matrix - matrix[first]) ** 2).sum(axis=1) * weights
        if distance.sum() <= 0:
            break
        second = rng.choice(len(matrix), p=distance / distance.sum())
        labels, inertia = _refine(matrix, weights, np.stack([matrix[first], matrix[second]]))
        if inertia < best_inertia:
            best_labels, best_inertia = labels, inertia
    return best_labels


def _refine(
    matrix: np.ndarray, weights: np.ndarray, centers: np.ndarray
) -> tuple[np.ndarray, float]:
    labels = np.zeros(len(matrix), dtype=int)
    for _ in range(50):
        labels = ((matrix[:, None, :] - centers[None]) ** 2).sum(axis=-1).argmin(axis=1)
        updated = np.stack(
            [
                np.average(matrix[labels == k], axis=0, weights=weights[labels == k])
                if (labels == k).any()
                else centers[k]
                for k in (0, 1)
            ]
        )
        if np.allclose(updated, centers):
            break
        centers = updated
    inertia = sum(
        float(((matrix[labels == k] - centers[k]) ** 2).sum(axis=1) @ weights[labels == k])
        for k in (0, 1)
    )
    return labels, inertia
