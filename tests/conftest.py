import json
from pathlib import Path

import pytest

from callscope.model import Dialogue
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
