"""Генератор тестовых звонков: диалоги из dialogues.yaml озвучиваются `say` (macOS).

Менеджер звучит в левом канале, клиент — в правом. Используются только встроенные
утилиты macOS (`say`, `afconvert`) и стандартная библиотека; PyYAML уже есть в окружении.

Запуск: uv run python scripts/generate_examples.py [--out examples/calls]
"""

from __future__ import annotations

import argparse
import array
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DIALOGUES_PATH = HERE / "dialogues.yaml"
DEFAULT_OUT = HERE.parent / "examples" / "calls"
SAMPLE_RATE = 16000
PAUSE_SECONDS = 0.6
LEAD_SECONDS = 0.3
# Если русский голос один, стороны различаются скоростью речи (слов в минуту).
MANAGER_RATE = 165
CLIENT_RATE = 205
WORDS_PER_SECOND = 2.6  # для оценки времени реплик в тексте без аудио


class GeneratorError(Exception):
    """Ошибка генерации с понятным сообщением на русском."""


def load_calls(path: Path = DIALOGUES_PATH) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list(data["calls"])


def call_lines(call: dict) -> list[tuple[str, str]]:
    """Реплики звонка как пары (роль, текст), роль — manager или client."""
    lines = []
    for line in call["lines"]:
        (role, text), = line.items()
        lines.append((role, text))
    return lines


def build_dialogue_json(call: dict) -> dict:
    """Диалог в формате Dialogue.from_json с приблизительным временем реплик (без аудио)."""
    clock = LEAD_SECONDS
    utterances = []
    for index, (role, text) in enumerate(call_lines(call)):
        duration = max(1.0, len(text.split()) / WORDS_PER_SECOND)
        utterances.append(
            {
                "index": index,
                "start": round(clock, 2),
                "end": round(clock + duration, 2),
                "text": text,
                "role": role,
            }
        )
        clock += duration + PAUSE_SECONDS
    return {
        "approximate": False,
        "manager_channel": "left",
        "role_source": "intro",
        "utterances": utterances,
    }


def russian_voices() -> list[str]:
    if shutil.which("say") is None or shutil.which("afconvert") is None:
        raise GeneratorError(
            "Нужны встроенные утилиты macOS `say` и `afconvert`: запустите генератор на macOS."
        )
    result = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True)
    voices = []
    for row in result.stdout.splitlines():
        match = re.match(r"^(.+?)\s+ru[_-]RU\b", row)
        if match:
            voices.append(match.group(1).strip())
    if not voices:
        raise GeneratorError(
            "В системе нет русского голоса для `say`. Добавьте его: "
            "Системные настройки → Универсальный доступ → Речь → Системный голос."
        )
    return voices


def voice_plan(voices: list[str]) -> dict[str, tuple[str, int]]:
    """Голос и скорость для каждой стороны: все русские голоса по кругу, иначе — разная скорость."""
    if len(voices) == 1:
        return {"manager": (voices[0], MANAGER_RATE), "client": (voices[0], CLIENT_RATE)}
    return {"manager": (voices[0], MANAGER_RATE), "client": (voices[1], MANAGER_RATE)}


def speak(text: str, voice: str, rate: int, workdir: Path, name: str) -> list[int]:
    aiff = workdir / f"{name}.aiff"
    wav = workdir / f"{name}.wav"
    subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", str(aiff), text], check=True)
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", str(aiff), str(wav)],
        check=True,
    )
    with wave.open(str(wav), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()
    return list(samples)


def write_stereo_wav(path: Path, left: list[int], right: list[int]) -> None:
    interleaved = array.array("h")
    for l_sample, r_sample in zip(left, right, strict=True):
        interleaved.append(l_sample)
        interleaved.append(r_sample)
    if sys.byteorder == "big":
        interleaved.byteswap()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(interleaved.tobytes())


def render_call(call: dict, plan: dict[str, tuple[str, int]], out_dir: Path) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        left: list[int] = [0] * int(LEAD_SECONDS * SAMPLE_RATE)
        right: list[int] = [0] * len(left)
        pause = [0] * int(PAUSE_SECONDS * SAMPLE_RATE)
        for index, (role, text) in enumerate(call_lines(call)):
            voice, rate = plan[role]
            samples = speak(text, voice, rate, workdir, f"line{index}")
            silence = [0] * len(samples)
            if role == "manager":
                left += samples
                right += silence
            else:
                left += silence
                right += samples
            left += pause
            right += pause
        stereo = workdir / "call.wav"
        write_stereo_wav(stereo, left, right)
        target = out_dir / f"{call['file']}.m4a"
        subprocess.run(
            ["afconvert", "-f", "m4af", "-d", "aac", "-b", "48000", str(stereo), str(target)],
            check=True,
        )
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Генератор тестовых звонков CallScope")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="папка для m4a")
    args = parser.parse_args()
    try:
        plan = voice_plan(russian_voices())
        args.out.mkdir(parents=True, exist_ok=True)
        for call in load_calls():
            target = render_call(call, plan, args.out)
            print(f"{target.name}: {call['title']}")
    except GeneratorError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
