import io
import json
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from callscope.checklist import load_checklist
from callscope.config import DEFAULTS_DIR, SemanticConfig, Thresholds, load_config
from callscope.model import CallScore, Dialogue, ItemResult
from callscope.semantic import recheck

THRESHOLDS = Thresholds()

DIALOGUE = Dialogue.from_json(
    {
        "approximate": False,
        "manager_channel": "left",
        "role_source": "intro",
        "utterances": [
            {
                "start": 0.0,
                "end": 3.0,
                "role": "manager",
                "text": "Здравствуйте, меня зовут Анна, компания СтройДом.",
            },
            {"start": 3.5, "end": 6.0, "role": "client", "text": "Мне нужен ламинат на кухню."},
            {
                "start": 6.5,
                "end": 9.0,
                "role": "manager",
                "text": "Расскажите, пожалуйста, про вашу кухню подробнее — площадь, основание.",
            },
            {"start": 9.5, "end": 12.0, "role": "client", "text": "Метров двенадцать, бетон."},
            {
                "start": 12.5,
                "end": 15.0,
                "role": "manager",
                "text": "Предлагаю влагостойкий ламинат тридцать третьего класса.",
            },
            {"start": 15.5, "end": 17.0, "role": "manager", "text": "Всего доброго, до свидания!"},
        ],
    }
)


def _item(item_id, status):
    return ItemResult(item_id=item_id, status=status, reason="по ключевым фразам", evidence=())


# Веса по умолчанию: greeting 5, need 20, solution 15, objection 30, next_step 25, farewell 5.
# Применимы 70; выполнено 5 + 15 + 5 = 25 → 35.7.
SCORE = CallScore(
    items=(
        _item("greeting", "passed"),
        _item("need", "failed"),
        _item("solution", "passed"),
        _item("objection", "na"),
        _item("next_step", "failed"),
        _item("farewell", "passed"),
    ),
    score=35.7,
    loss_item_id="need",
    grade="bad",
)


@pytest.fixture
def checklist():
    return load_checklist(DEFAULTS_DIR / "checklist.yaml")


@pytest.fixture
def config():
    cfg = load_config(DEFAULTS_DIR / "callscope.yaml")
    return replace(cfg, semantic=SemanticConfig(enabled=True))


class FakeOllama:
    """Подменяет urllib.request.urlopen: отвечает по названию пункта в промпте."""

    def __init__(self, answers):
        self.answers = answers  # title-фрагмент → dict ответа модели
        self.requests = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        self.requests.append((request.full_url, body))
        answer = {"passed": False, "quote": "", "reason": "не найдено"}
        for fragment, value in self.answers.items():
            if fragment in body["prompt"]:
                answer = value
        payload = json.dumps({"response": json.dumps(answer, ensure_ascii=False)})
        return io.BytesIO(payload.encode("utf-8"))


def _by_id(score, item_id):
    return next(r for r in score.items if r.item_id == item_id)


def test_model_verdict_with_real_quote_is_accepted_and_score_recomputed(
    checklist, config, monkeypatch
):
    fake = FakeOllama(
        {
            "Выявление потребности": {
                "passed": True,
                "quote": "расскажите пожалуйста про вашу кухню подробнее",
                "reason": "менеджер расспросил о кухне",
            }
        }
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    new, warnings = recheck(DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    need = _by_id(new, "need")
    assert need.status == "passed" and need.by_model is True
    assert [e.utterance_index for e in need.evidence] == [2]
    # 45 / 70 * 100
    assert new.score == pytest.approx(64.3, abs=0.05)
    assert new.loss_item_id == "next_step"
    assert new.grade == "medium"
    assert warnings == []
    assert all(url.startswith("http://localhost:11434/") for url, _ in fake.requests)


def test_only_failed_items_are_sent(checklist, config, monkeypatch):
    fake = FakeOllama({})
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    new, _ = recheck(DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    assert len(fake.requests) == 2
    assert new == SCORE


def test_quote_absent_from_transcript_is_rejected(checklist, config, monkeypatch):
    fake = FakeOllama(
        {
            "Выявление потребности": {
                "passed": True,
                "quote": "Какой у вас бюджет и сроки?",
                "reason": "спросил бюджет",
            }
        }
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    new, _ = recheck(DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    assert _by_id(new, "need").status == "failed"
    assert _by_id(new, "need").by_model is False
    assert new.score == SCORE.score


def test_quote_from_wrong_speaker_is_rejected(checklist, config, monkeypatch):
    fake = FakeOllama(
        {
            "Выявление потребности": {
                "passed": True,
                "quote": "Мне нужен ламинат на кухню",
                "reason": "потребность ясна",
            }
        }
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    new, _ = recheck(DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    assert _by_id(new, "need").status == "failed"


def test_connection_refused_keeps_original_score_with_warning(checklist, config, monkeypatch):
    def refuse(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    new, warnings = recheck(DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    assert new == SCORE
    assert len(warnings) == 1 and "Ollama" in warnings[0]


def test_non_local_url_is_refused_without_request(checklist, config, monkeypatch):
    fake = FakeOllama({})
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    remote = replace(config, semantic=SemanticConfig(enabled=True, url="http://example.com:11434"))
    new, warnings = recheck(DIALOGUE, checklist, SCORE, remote, THRESHOLDS)
    assert fake.requests == []
    assert new == SCORE
    assert len(warnings) == 1 and "example.com" in warnings[0]


def test_garbage_model_answer_keeps_item_failed(checklist, config, monkeypatch):
    def garbage(request, timeout=None):
        return io.BytesIO(json.dumps({"response": "это не json"}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", garbage)
    new, warnings = recheck(DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    assert new == SCORE
    assert warnings
