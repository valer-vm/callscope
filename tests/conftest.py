import json
from pathlib import Path

import pytest

from callscope.errors import AudioError
from callscope.model import Dialogue, RawTranscript
from callscope.textnorm import Normalizer

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def normalizer() -> Normalizer:
    return Normalizer()


@pytest.fixture
def load_dialogue():
    def _load(name: str) -> Dialogue:
        data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        return Dialogue.from_json(data)

    return _load


def raw_from_dialogue_file(name: str) -> RawTranscript:
    """Сырая стерео-расшифровка из JSON-фикстуры диалога: менеджер слева, клиент справа."""
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    segments = [
        {
            "start": u["start"],
            "end": u["end"],
            "text": u["text"],
            "channel": "left" if u["role"] == "manager" else "right",
        }
        for u in data["utterances"]
    ]
    duration = segments[-1]["end"] if segments else 0.0
    return RawTranscript.from_json({"mode": "stereo", "duration": duration, "segments": segments})


@pytest.fixture
def raw_from_fixture():
    return raw_from_dialogue_file


class FakeTranscriber:
    """Подменённая расшифровка: «аудио» — имя JSON-фикстуры, `BROKEN` — битый файл."""

    def __init__(self, model_size: str = "small", model_path: str | None = None) -> None:
        self.model_size = model_size
        self.model_path = model_path
        self.calls: list[str] = []

    def transcribe(self, path: Path, mono_turn_gap: float, on_progress=None) -> RawTranscript:
        self.calls.append(path.name)
        # Первая строка — фикстура; дальше можно дописать что угодно, чтобы sha256 различался.
        content = path.read_text(encoding="utf-8").strip().splitlines()[0]
        if content == "BROKEN":
            raise AudioError(f"Не удалось прочитать запись {path.name}: файл повреждён.")
        if on_progress:
            on_progress(0.5)
        return raw_from_dialogue_file(content)


@pytest.fixture
def fake_transcriber_cls():
    return FakeTranscriber
