"""Смысловая перепроверка пунктов локальной моделью Ollama.

Только HTTP на localhost стандартной библиотекой. Модель перепроверяет пункты, проваленные
по ключевым фразам, и условные пункты, условие которых по фразам не сработало
(возражение сказано другими словами). Любой вердикт принимается, только если модель
привела цитату, которая действительно есть в репликах нужной стороны.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any

from callscope.checklist import Checklist, ChecklistItem
from callscope.config import AppConfig, Thresholds, is_local_url
from callscope.model import CallScore, Dialogue, Evidence, ItemResult, Utterance
from callscope.scoring import summarize

_ROLE_NAMES = {"manager": "Менеджер", "client": "Клиент"}
_OTHER_ROLE = {"manager": "client", "client": "manager"}
_NON_WORD = re.compile(r"[^0-9a-zа-я]+")
_MIN_QUOTE_LETTERS = 3

_PROMPT = """Ты проверяешь телефонный звонок отдела продаж по одному пункту чек-листа.
Пункт: {title}
Что значит «выполнен»: {description}
Чью речь проверять: {speaker}.

Расшифровка звонка (номер реплики, сторона, текст):
{transcript}

Ответь строго JSON-объектом без пояснений вокруг:
{{"passed": true или false, "quote": "точная цитата из реплики, которая доказывает выполнение \
(пустая строка, если не выполнен)", "reason": "короткое объяснение по-русски"}}
"""

_TRIGGER_PROMPT = """Ты проверяешь телефонный звонок отдела продаж по одному пункту чек-листа.
Пункт: {title}
Что значит «выполнен»: {description}
Пункт проверяется, только если собеседник ({other}) сказал что-то такое, например: {examples}.
Таких слов дословно нет — проверь по смыслу. Считай только явный случай того же смысла;
простое согласие, вопрос или уточнение — не оно. Сомневаешься — "triggered": false.
Если такое было, проверь, ответил ли {speaker} по существу в ближайших репликах.

Расшифровка звонка (номер реплики, сторона, текст):
{transcript}

Ответь строго JSON-объектом без пояснений вокруг:
{{"triggered": true или false, "trigger_quote": "точная цитата из реплики собеседника \
(пустая строка, если такого не было)", "passed": true или false, "quote": "точная цитата \
ответа (пустая строка, если ответа по существу нет)", "reason": "короткое объяснение по-русски"}}
"""


class _OllamaUnavailable(Exception):
    """Ollama не отвечает: дальнейшие запросы бессмысленны."""


def _normalize(text: str) -> str:
    """Нижний регистр, ё→е, слова через один пробел — для сверки цитаты."""
    return " ".join(_NON_WORD.sub(" ", text.lower().replace("ё", "е")).split())


def _transcript(dialogue: Dialogue) -> str:
    return "\n".join(f"[{u.index}] {_ROLE_NAMES[u.role]}: {u.text}" for u in dialogue.utterances)


def _read(request: urllib.request.Request, timeout: float) -> bytes:
    # Без прокси: urlopen берёт HTTP_PROXY из окружения даже для localhost,
    # и записи ушли бы на чужой сервер.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return response.read()


@dataclass(frozen=True)
class OllamaStatus:
    """Ответ Ollama на «какие модели установлены»; `error` — почему не ответила."""

    reachable: bool
    models: tuple[str, ...] = ()
    error: str | None = None


def ollama_status(url: str, timeout: float = 3) -> OllamaStatus:
    """Работает ли Ollama по адресу и какие модели в ней скачаны."""
    if not is_local_url(url):
        return OllamaStatus(False, error="адрес не на этом компьютере")
    request = urllib.request.Request(url.rstrip("/") + "/api/tags", method="GET")
    try:
        data = json.loads(_read(request, timeout).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return OllamaStatus(False, error=f"ответ HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        return OllamaStatus(False, error=str(getattr(exc, "reason", exc)))
    except (ValueError, UnicodeDecodeError):
        return OllamaStatus(False, error="непонятный ответ")
    models = data.get("models") if isinstance(data, dict) else None
    names = [m.get("name") for m in models or () if isinstance(m, dict)]
    return OllamaStatus(True, tuple(sorted(n for n in names if isinstance(n, str))))


def _ask(config: AppConfig, prompt: str) -> Any:
    """Один запрос к Ollama; возвращает разобранный JSON-ответ модели или None."""
    semantic = config.semantic
    body = json.dumps(
        {
            "model": semantic.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        semantic.url.rstrip("/") + "/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        raw = _read(request, semantic.timeout)
    except urllib.error.HTTPError as exc:
        raise _OllamaUnavailable(f"ответ HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise _OllamaUnavailable(str(reason)) from None
    try:
        envelope = json.loads(raw.decode("utf-8"))
        return json.loads(envelope["response"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None


def _find_quote(quote: Any, dialogue: Dialogue, role: str, after: int = -1) -> Utterance | None:
    """Реплика стороны `role` (позже реплики `after`), в которой есть цитата модели."""
    if not isinstance(quote, str):
        return None
    needle = _normalize(quote)
    if len(needle.replace(" ", "")) < _MIN_QUOTE_LETTERS:
        return None
    for utterance in dialogue.utterances:
        if utterance.role != role or utterance.index <= after:
            continue
        if f" {needle} " in f" {_normalize(utterance.text)} ":
            return utterance
    return None


def recheck(
    dialogue: Dialogue,
    checklist: Checklist,
    score: CallScore,
    config: AppConfig,
    thresholds: Thresholds,
) -> tuple[CallScore, list[str]]:
    """Перепроверяет провалы и несработавшие условия.

    Включённость (`semantic.enabled`) решает вызывающий.
    """
    url = config.semantic.url
    if not is_local_url(url):
        return score, [
            f"Смысловая проверка пропущена: адрес Ollama {url} не локальный "
            "(допустимы только localhost и 127.0.0.1) — записи не отправляются наружу."
        ]
    by_id = {item.id: item for item in checklist.items}
    candidates = [
        r
        for r in score.items
        if r.item_id in by_id
        and (r.status == "failed" or (r.status == "na" and by_id[r.item_id].trigger))
    ]
    if not candidates:
        return score, []

    transcript = _transcript(dialogue)
    warnings: list[str] = []
    updated: dict[str, ItemResult] = {}
    for result in candidates:
        item = by_id[result.item_id]
        if result.status == "na":
            prompt = _TRIGGER_PROMPT.format(
                title=item.title,
                description=item.description or item.title,
                other=_ROLE_NAMES[_OTHER_ROLE[item.speaker]].lower(),
                examples=", ".join(f"«{phrase}»" for phrase in item.trigger[:6]),
                speaker=_ROLE_NAMES[item.speaker].lower(),
                transcript=transcript,
            )
        else:
            prompt = _PROMPT.format(
                title=item.title,
                description=item.description or item.title,
                speaker=_ROLE_NAMES[item.speaker],
                transcript=transcript,
            )
        try:
            answer = _ask(config, prompt)
        except _OllamaUnavailable as exc:
            return score, [
                f"Ollama недоступен по адресу {url} ({exc}) — смысловая проверка пропущена, "
                "оценка по ключевым фразам."
            ]
        if not isinstance(answer, dict):
            warnings.append(
                f"Ollama: ответ по пункту «{item.title}» не разобран — пункт не изменён."
            )
            continue
        if result.status == "na":
            verdict = _trigger_verdict(answer, result, dialogue, item)
        else:
            verdict = _failed_verdict(answer, result, dialogue, item)
        if verdict is not None:
            updated[result.item_id] = verdict

    if not updated:
        return score, warnings
    items = tuple(updated.get(r.item_id, r) for r in score.items)
    return summarize(items, checklist, thresholds), warnings


def _reason(prefix: str, answer: dict[str, Any]) -> str:
    model_reason = answer.get("reason")
    if isinstance(model_reason, str) and model_reason.strip():
        return f"{prefix}: {model_reason.strip()}"
    return prefix


def _evidence(utterance: Utterance) -> Evidence:
    return Evidence(
        utterance_index=utterance.index,
        start=utterance.start,
        role=utterance.role,
        text=utterance.text,
    )


def _failed_verdict(
    answer: dict[str, Any], result: ItemResult, dialogue: Dialogue, item: ChecklistItem
) -> ItemResult | None:
    """Проваленный пункт: «выполнен» — только с цитатой из реплик нужной стороны."""
    if answer.get("passed") is not True:
        return None
    utterance = _find_quote(answer.get("quote"), dialogue, item.speaker)
    if utterance is None:
        return None  # цитаты нет в репликах — вердикт модели отклонён
    return replace(
        result,
        status="passed",
        reason=_reason("Подтверждено моделью", answer),
        evidence=(_evidence(utterance),),
        by_model=True,
    )


def _trigger_verdict(
    answer: dict[str, Any], result: ItemResult, dialogue: Dialogue, item: ChecklistItem
) -> ItemResult | None:
    """Условие не сработало по фразам: модель могла найти его по смыслу.

    Без настоящей цитаты собеседника пункт остаётся «не применим». С ней — «выполнен»,
    если есть цитата ответа нужной стороны после неё, иначе «не выполнен».
    """
    if answer.get("triggered") is not True:
        return None
    trigger = _find_quote(answer.get("trigger_quote"), dialogue, _OTHER_ROLE[item.speaker])
    if trigger is None:
        return None
    reply = None
    if answer.get("passed") is True:
        reply = _find_quote(answer.get("quote"), dialogue, item.speaker, after=trigger.index)
    if reply is None:
        return replace(
            result,
            status="failed",
            reason=_reason(
                f"Модель нашла возражение «{trigger.text}» без ответа по существу", answer
            ),
            evidence=(_evidence(trigger),),
            by_model=True,
        )
    return replace(
        result,
        status="passed",
        reason=_reason(f"Модель нашла возражение «{trigger.text}» и ответ на него", answer),
        evidence=(_evidence(trigger), _evidence(reply)),
        by_model=True,
    )
