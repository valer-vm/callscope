"""Смысловая перепроверка проваленных пунктов локальной моделью Ollama.

Только HTTP на localhost стандартной библиотекой. Модель перепроверяет лишь пункты,
проваленные по ключевым фразам; вердикт «выполнен» принимается, только если модель
привела цитату, которая действительно есть в репликах нужной стороны.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Any

from callscope.checklist import Checklist, ChecklistItem
from callscope.config import AppConfig, Thresholds, is_local_url
from callscope.model import CallScore, Dialogue, Evidence, ItemResult, Utterance
from callscope.scoring import grade_for

_ROLE_NAMES = {"manager": "Менеджер", "client": "Клиент"}
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


class _OllamaUnavailable(Exception):
    """Ollama не отвечает: дальнейшие запросы бессмысленны."""


def _normalize(text: str) -> str:
    """Нижний регистр, ё→е, слова через один пробел — для сверки цитаты."""
    return " ".join(_NON_WORD.sub(" ", text.lower().replace("ё", "е")).split())


def _transcript(dialogue: Dialogue) -> str:
    return "\n".join(f"[{u.index}] {_ROLE_NAMES[u.role]}: {u.text}" for u in dialogue.utterances)


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
        # Без прокси: urlopen берёт HTTP_PROXY из окружения даже для localhost,
        # и записи ушли бы на чужой сервер.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=semantic.timeout) as response:
            raw = response.read()
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


def _find_quote(quote: str, dialogue: Dialogue, item: ChecklistItem) -> Utterance | None:
    needle = _normalize(quote)
    if len(needle.replace(" ", "")) < _MIN_QUOTE_LETTERS:
        return None
    for utterance in dialogue.utterances:
        if utterance.role != item.speaker:
            continue
        if f" {needle} " in f" {_normalize(utterance.text)} ":
            return utterance
    return None


def _rescore(
    items: tuple[ItemResult, ...], checklist: Checklist, thresholds: Thresholds
) -> CallScore:
    weights = {item.id: item.weight for item in checklist.items}
    applicable = sum(weights.get(r.item_id, 0.0) for r in items if r.status != "na")
    passed = sum(weights.get(r.item_id, 0.0) for r in items if r.status == "passed")
    score = round(passed / applicable * 100, 1) if applicable else 0.0
    loss = next((r.item_id for r in items if r.status == "failed"), None)
    return CallScore(
        items=items, score=score, loss_item_id=loss, grade=grade_for(score, thresholds)
    )


def recheck(
    dialogue: Dialogue,
    checklist: Checklist,
    score: CallScore,
    config: AppConfig,
    thresholds: Thresholds,
) -> tuple[CallScore, list[str]]:
    """Перепроверяет проваленные пункты. Включённость (`semantic.enabled`) решает вызывающий."""
    url = config.semantic.url
    if not is_local_url(url):
        return score, [
            f"Смысловая проверка пропущена: адрес Ollama {url} не локальный "
            "(допустимы только localhost и 127.0.0.1) — записи не отправляются наружу."
        ]
    by_id = {item.id: item for item in checklist.items}
    failed = [r for r in score.items if r.status == "failed" and r.item_id in by_id]
    if not failed:
        return score, []

    transcript = _transcript(dialogue)
    warnings: list[str] = []
    updated: dict[str, ItemResult] = {}
    for result in failed:
        item = by_id[result.item_id]
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
        if answer.get("passed") is not True:
            continue
        quote = answer.get("quote")
        utterance = _find_quote(quote, dialogue, item) if isinstance(quote, str) else None
        if utterance is None:
            continue  # цитаты нет в репликах — вердикт модели отклонён
        model_reason = answer.get("reason")
        reason = "Подтверждено моделью"
        if isinstance(model_reason, str) and model_reason.strip():
            reason += f": {model_reason.strip()}"
        updated[result.item_id] = replace(
            result,
            status="passed",
            reason=reason,
            evidence=(
                Evidence(
                    utterance_index=utterance.index,
                    start=utterance.start,
                    role=utterance.role,
                    text=utterance.text,
                ),
            ),
            by_model=True,
        )

    if not updated:
        return score, warnings
    items = tuple(updated.get(r.item_id, r) for r in score.items)
    return _rescore(items, checklist, thresholds), warnings
