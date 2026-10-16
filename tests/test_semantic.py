import http.server
import io
import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from callscope.checklist import load_checklist
from callscope.config import DEFAULTS_DIR, SemanticConfig, Thresholds, load_config
from callscope.model import CallScore, Dialogue, ItemResult
from callscope.semantic import ollama_status, recheck

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
    """Поддельная Ollama: отвечает по названию пункта в промпте."""

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


def _patch_open(monkeypatch, fake):
    """Подменяет отправку запроса любым urllib-открывателем (urlopen и build_opener)."""
    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda self, request, data=None, timeout=None: fake(request, timeout=timeout),
    )


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
    _patch_open(monkeypatch, fake)
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


def test_failed_and_untriggered_items_are_sent(checklist, config, monkeypatch):
    fake = FakeOllama({})
    _patch_open(monkeypatch, fake)
    new, _ = recheck(DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    prompts = [body["prompt"] for _, body in fake.requests]
    # need и next_step провалены; objection «не применим», но у него есть условие
    assert len(prompts) == 3
    assert sum("Отработка возражения" in p for p in prompts) == 1
    assert not any("Приветствие" in p for p in prompts)
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
    _patch_open(monkeypatch, fake)
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
    _patch_open(monkeypatch, fake)
    new, _ = recheck(DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    assert _by_id(new, "need").status == "failed"


def test_connection_refused_keeps_original_score_with_warning(checklist, config, monkeypatch):
    def refuse(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    _patch_open(monkeypatch, refuse)
    new, warnings = recheck(DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    assert new == SCORE
    assert len(warnings) == 1 and "Ollama" in warnings[0]


def test_non_local_url_is_refused_without_request(checklist, config, monkeypatch):
    fake = FakeOllama({})
    _patch_open(monkeypatch, fake)
    remote = replace(config, semantic=SemanticConfig(enabled=True, url="http://example.com:11434"))
    new, warnings = recheck(DIALOGUE, checklist, SCORE, remote, THRESHOLDS)
    assert fake.requests == []
    assert new == SCORE
    assert len(warnings) == 1 and "example.com" in warnings[0]


def test_garbage_model_answer_keeps_item_failed(checklist, config, monkeypatch):
    def garbage(request, timeout=None):
        return io.BytesIO(json.dumps({"response": "это не json"}).encode("utf-8"))

    _patch_open(monkeypatch, garbage)
    new, warnings = recheck(DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    assert new == SCORE
    assert warnings


def test_request_goes_direct_even_with_proxy_configured(checklist, config, monkeypatch):
    """Записи не уходят через системный прокси: Ollama на localhost — только напрямую."""
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            answer = {"passed": False, "quote": "", "reason": "не найдено"}
            payload = json.dumps({"response": json.dumps(answer, ensure_ascii=False)})
            data = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
            monkeypatch.setenv(name, "http://127.0.0.1:9")
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        url = f"http://localhost:{server.server_address[1]}"
        local = replace(config, semantic=SemanticConfig(enabled=True, url=url, timeout=5))
        new, warnings = recheck(DIALOGUE, checklist, SCORE, local, THRESHOLDS)
    finally:
        server.shutdown()
        server.server_close()
    assert warnings == []
    assert received == ["/api/generate"] * 3
    assert new == SCORE


# --- условие, не пойманное фразами: возражение другими словами -------------------------

OBJECTION_DIALOGUE = Dialogue.from_json(
    {
        "approximate": False,
        "manager_channel": "left",
        "role_source": "intro",
        "utterances": [
            {"start": 0.0, "end": 3.0, "role": "manager", "text": "Здравствуйте, Анна, СтройДом."},
            {"start": 3.5, "end": 6.0, "role": "client", "text": "Мне нужен ламинат на кухню."},
            {
                "start": 6.5,
                "end": 9.0,
                "role": "manager",
                "text": "Есть хороший влагостойкий, тысяча двести за метр.",
            },
            {
                "start": 9.5,
                "end": 12.0,
                "role": "client",
                "text": "В соседней фирме мне на треть меньше насчитали.",
            },
            {
                "start": 12.5,
                "end": 15.0,
                "role": "manager",
                "text": "У нас укладка уже входит в цену, отсюда и разница.",
            },
            {"start": 15.5, "end": 17.0, "role": "manager", "text": "Всего доброго, до свидания!"},
        ],
    }
)

TRIGGER_PROMPT_MARK = "Таких слов дословно нет"


def _objection_answer(**fields):
    answer = {"triggered": True, "trigger_quote": "", "passed": False, "quote": "", "reason": ""}
    answer.update(fields)
    return FakeOllama({TRIGGER_PROMPT_MARK: answer})


def test_untriggered_objection_found_and_answered_passes(checklist, config, monkeypatch):
    fake = _objection_answer(
        trigger_quote="на треть меньше насчитали",
        passed=True,
        quote="укладка уже входит в цену",
        reason="сравнение с конкурентом, ответ про укладку",
    )
    _patch_open(monkeypatch, fake)
    new, warnings = recheck(OBJECTION_DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    objection = _by_id(new, "objection")
    assert objection.status == "passed" and objection.by_model is True
    assert [e.utterance_index for e in objection.evidence] == [3, 4]
    assert "соседней фирме" in objection.reason
    # 5 + 15 + 30 + 5 из 100 применимых
    assert new.score == pytest.approx(55.0)
    assert warnings == []


def test_untriggered_objection_without_answer_fails(checklist, config, monkeypatch):
    fake = _objection_answer(trigger_quote="в соседней фирме мне на треть меньше насчитали")
    _patch_open(monkeypatch, fake)
    new, _ = recheck(OBJECTION_DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    objection = _by_id(new, "objection")
    assert objection.status == "failed" and objection.by_model is True
    assert [e.utterance_index for e in objection.evidence] == [3]
    assert new.score == pytest.approx(25.0)


def test_answer_quoted_before_objection_does_not_count(checklist, config, monkeypatch):
    fake = _objection_answer(
        trigger_quote="на треть меньше насчитали", passed=True, quote="хороший влагостойкий"
    )
    _patch_open(monkeypatch, fake)
    new, _ = recheck(OBJECTION_DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    assert _by_id(new, "objection").status == "failed"


@pytest.mark.parametrize(
    "trigger_quote",
    [
        "мне это дорого",  # такой реплики нет
        "укладка уже входит в цену",  # реплика менеджера, а не клиента
        "",
    ],
)
def test_objection_without_real_client_quote_stays_not_applicable(
    checklist, config, monkeypatch, trigger_quote
):
    fake = _objection_answer(trigger_quote=trigger_quote, passed=True, quote="входит в цену")
    _patch_open(monkeypatch, fake)
    new, _ = recheck(OBJECTION_DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    assert _by_id(new, "objection").status == "na"
    assert new.score == SCORE.score


def test_model_says_no_objection_keeps_not_applicable(checklist, config, monkeypatch):
    fake = _objection_answer(triggered=False, trigger_quote="на треть меньше насчитали")
    _patch_open(monkeypatch, fake)
    new, _ = recheck(OBJECTION_DIALOGUE, checklist, SCORE, config, THRESHOLDS)
    assert _by_id(new, "objection").status == "na"


# --- состояние Ollama для страницы настроек ------------------------------------------


def test_ollama_status_lists_models(monkeypatch):
    seen = []

    def tags(request, timeout=None):
        seen.append(request.full_url)
        body = {"models": [{"name": "qwen2.5:7b"}, {"name": "qwen2.5:3b"}, {"size": 1}]}
        return io.BytesIO(json.dumps(body).encode("utf-8"))

    _patch_open(monkeypatch, tags)
    status = ollama_status("http://localhost:11434/")
    assert status.reachable and status.models == ("qwen2.5:3b", "qwen2.5:7b")
    assert seen == ["http://localhost:11434/api/tags"]


def test_ollama_status_when_refused(monkeypatch):
    def refuse(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    _patch_open(monkeypatch, refuse)
    status = ollama_status("http://localhost:11434")
    assert not status.reachable and "refused" in status.error


def test_ollama_status_never_asks_remote_address(monkeypatch):
    fake = FakeOllama({})
    _patch_open(monkeypatch, fake)
    assert not ollama_status("http://example.com:11434").reachable
    assert fake.requests == []
