"""Оценка звонка по чек-листу: ключевые фразы по леммам и правила, без нейросети."""

from __future__ import annotations

import re
from functools import lru_cache
from itertools import islice

from callscope.checklist import Checklist, ChecklistItem
from callscope.config import Thresholds
from callscope.model import CallScore, Dialogue, Evidence, Grade, ItemResult, Utterance
from callscope.textnorm import Normalizer

_ROLE_GENITIVE = {"manager": "менеджера", "client": "клиента"}
_MAX_VARIANTS = 64
_MAX_EVIDENCE = 3
_SENTENCE = re.compile(r"[^.!?…]+[.!?…]*")
# Леммы, которые можно пропустить перед вопросительным словом:
# «А какой…», «Скажите, сколько…», «Для чего…».
_QUESTION_LEAD_INS = frozenset(
    "а и но так ну вот сказать подсказывать уточнять "
    "в во на для из с со по к о об у под за до при от".split()
)
# Отрицание прямо перед фразой («не бесплатно», «нет скидок», «без гарантии»)
# или «нет» в ближайших словах после неё в том же предложении («скидок у нас нет»).
_NEGATIONS_BEFORE = frozenset({"не", "нет", "без", "ни"})
_NEGATION_AFTER = "нет"
_NEGATION_AFTER_WINDOW = 3
_CLAUSE_BREAK = re.compile(r"[,;:—–()]")
# Вводные слова между запятыми не отрывают «нет» от фразы: «Скидок, к сожалению, нет».
_PARENTHETICALS = (
    "к сожалению",
    "увы",
    "конечно",
    "правда",
    "честно говоря",
    "честно",
    "наверное",
    "видимо",
    "кстати",
    "впрочем",
    "собственно",
    "в общем",
    "в принципе",
    "знаете",
    "понимаете",
    "извините",
    "простите",
    "к счастью",
    "по-моему",
    "вообще-то",
    "думаю",
)
# Что может стоять перед «нет» после вводных слов, чтобы «нет» ещё относилось к фразе
# («Скидок, к сожалению, у нас сейчас нет»). Новое существительное («…, переплат нет»)
# сюда не входит — там «нет» про него.
_NEGATION_FILLERS = frozenset(
    "у мы я вы они он она оно сейчас пока уже здесь тут вообще совсем больше "
    "сегодня теперь давно никакой".split()
)
# Фраза-триггер, за которой сразу идёт вопросительное слово, — уточнение, а не
# возражение: «не уверен, какая площадь». «Когда» сюда не входит: «подумаю, когда
# будут деньги» — условие, а не вопрос.
_CLARIFYING_WORDS = frozenset(
    "какой каков сколько где как куда откуда зачем почему который чей".split()
)
# statement_phrases не засчитываются в вопросе и рядом с отрицанием: «Вам нужен счёт?»,
# «Договор не нужен».
_STATEMENT_NEGATIONS = frozenset({"не", "нет", "без"})


@lru_cache(maxsize=1)
def _default_normalizer() -> Normalizer:
    return Normalizer()


def grade_for(score: float, thresholds: Thresholds) -> Grade:
    if score >= thresholds.good:
        return "good"
    if score >= thresholds.medium:
        return "medium"
    return "bad"


def _evidence(utterance: Utterance) -> Evidence:
    return Evidence(
        utterance_index=utterance.index,
        start=utterance.start,
        role=utterance.role,
        text=utterance.text,
    )


def _quote_list(phrases: tuple[str, ...], limit: int = 4) -> str:
    shown = ", ".join(f"«{p}»" for p in phrases[:limit])
    return shown + ("…" if len(phrases) > limit else "")


class _ItemScorer:
    def __init__(self, dialogue: Dialogue, checklist: Checklist, normalizer: Normalizer) -> None:
        self.dialogue = dialogue
        self.checklist = checklist
        self.norm = normalizer
        self.position = {u.index: pos for pos, u in enumerate(dialogue.utterances)}
        self.parentheticals = {tuple(normalizer.lemmas(p)) for p in _PARENTHETICALS}

    # --- фразы и синонимы ---------------------------------------------------

    def expand(self, phrases: tuple[str, ...], item: ChecklistItem) -> tuple[str, ...]:
        """Фразы пункта плюс варианты с заменой слов на синонимы (общие + пункта)."""
        synonyms = {**self.checklist.synonyms, **item.synonyms}
        rules = [
            (self.norm.lemmas(word), [self.norm.lemmas(r) for r in replacements])
            for word, replacements in synonyms.items()
        ]
        variants: list[list[str]] = [self.norm.lemmas(p) for p in phrases]
        seen = {tuple(v) for v in variants}
        queue = list(variants)
        while queue and len(variants) < _MAX_VARIANTS:
            current = queue.pop(0)
            for key, replacements in rules:
                if not key:
                    continue
                for pos in range(len(current) - len(key) + 1):
                    if current[pos : pos + len(key)] != key:
                        continue
                    for replacement in replacements:
                        candidate = current[:pos] + replacement + current[pos + len(key) :]
                        if tuple(candidate) not in seen and candidate:
                            seen.add(tuple(candidate))
                            variants.append(candidate)
                            queue.append(candidate)
        return tuple(" ".join(v) for v in variants if v)

    def matches(
        self,
        utterance: Utterance,
        phrases: tuple[str, ...],
        *,
        as_trigger: bool = False,
        statement_only: bool = False,
    ) -> bool:
        """Есть ли в реплике фраза без отрицания (для триггера — и не уточняющий вопрос;
        для statement_only — не в вопросе и без «не»/«нет» во всей части предложения)."""
        for sentence in _SENTENCE.findall(utterance.text):
            if statement_only and sentence.rstrip().endswith("?"):
                continue
            # Отрицание ищется только в той же части предложения (до запятой и т.п.):
            # «Есть рассрочка, переплат нет» — «нет» относится не к рассрочке.
            lemmas: list[str] = []
            clause_of: list[int] = []
            clauses = [self.norm.lemmas(c) for c in _CLAUSE_BREAK.split(sentence)]
            for clause_no, clause_lemmas in enumerate(clauses):
                lemmas.extend(clause_lemmas)
                clause_of.extend([clause_no] * len(clause_lemmas))
            for phrase in phrases:
                for start, end in self.norm.phrase_spans(sentence, phrase):
                    clause_end = end
                    while clause_end < len(lemmas) and clause_of[clause_end] == clause_of[end - 1]:
                        clause_end += 1
                    if (
                        start > 0
                        and clause_of[start - 1] == clause_of[start]
                        and lemmas[start - 1] in _NEGATIONS_BEFORE
                    ):
                        continue
                    after = lemmas[end : min(end + _NEGATION_AFTER_WINDOW, clause_end)]
                    if _NEGATION_AFTER in after:
                        continue
                    if (
                        clause_end - end < _NEGATION_AFTER_WINDOW
                        and self.negated_past_parenthetical(clauses, clause_of[end - 1])
                    ):
                        continue
                    if as_trigger and end < len(lemmas) and lemmas[end] in _CLARIFYING_WORDS:
                        continue
                    if statement_only and _STATEMENT_NEGATIONS.intersection(
                        clauses[clause_of[start]]
                    ):
                        continue
                    return True
        return False

    def negated_past_parenthetical(self, clauses: list[list[str]], clause_no: int) -> bool:
        """«нет» сразу за вводными словами, которые идут после части с фразой."""
        rest = [c for c in clauses[clause_no + 1 :] if c]
        skipped = 0
        while rest and tuple(rest[0]) in self.parentheticals:
            rest = rest[1:]
            skipped += 1
        if not skipped or not rest or _NEGATION_AFTER not in rest[0]:
            return False
        # «Скидок, к сожалению, нет» — отрицание; «Рассрочка, конечно, нет проблем» —
        # «нет» про проблемы, а не про рассрочку.
        position = rest[0].index(_NEGATION_AFTER)
        around = rest[0][:position] + rest[0][position + 1 :]
        return all(lemma in _NEGATION_FILLERS for lemma in around)

    # --- окна реплик --------------------------------------------------------

    def window(self, item: ChecklistItem) -> list[Utterance]:
        own = [u for u in self.dialogue.utterances if u.role == item.speaker]
        if item.window == "first":
            return own[: item.window_size]
        if item.window == "last":
            return own[-item.window_size :]
        return own

    def window_label(self, item: ChecklistItem) -> str:
        who = _ROLE_GENITIVE[item.speaker]
        if item.window == "first":
            return f"в первых {item.window_size} репликах {who}"
        if item.window == "last":
            return f"в последних {item.window_size} репликах {who}"
        return f"в репликах {who}"

    # --- правила ------------------------------------------------------------

    def score(self, item: ChecklistItem) -> ItemResult:
        if item.trigger:
            return self.score_conditional(item)
        if item.rule == "open_questions":
            return self.score_open_questions(item)
        return self.score_phrases(item)

    def score_phrases(self, item: ChecklistItem) -> ItemResult:
        phrases = self.expand(item.phrases, item)
        statements = self.expand(item.statement_phrases, item)
        window = self.window(item)
        found = [
            u
            for u in window
            if self.matches(u, phrases) or self.matches(u, statements, statement_only=True)
        ]
        where = self.window_label(item)
        if len(found) >= item.min_matches:
            return ItemResult(
                item_id=item.id,
                status="passed",
                reason=f"Найдено {where}: {len(found)} совп. (нужно {item.min_matches}).",
                evidence=tuple(_evidence(u) for u in found[:_MAX_EVIDENCE]),
            )
        if found:
            reason = (
                f"Ключевые фразы {where} найдены только в {len(found)} репл. "
                f"(нужно {item.min_matches})."
            )
            evidence = found
        else:
            all_phrases = item.phrases + item.statement_phrases
            reason = f"{where.capitalize()} нет ключевых фраз ({_quote_list(all_phrases)})."
            if item.window == "first":
                evidence = window[:1]
            elif item.window == "last":
                evidence = window[-1:]
            else:
                evidence = []
        if not window:
            reason = f"Нет реплик {_ROLE_GENITIVE[item.speaker]} для проверки."
        return ItemResult(
            item_id=item.id,
            status="failed",
            reason=reason,
            evidence=tuple(_evidence(u) for u in evidence[:_MAX_EVIDENCE]),
        )

    def is_open_question(self, sentence: str, question_words: tuple[str, ...]) -> bool:
        # Whisper часто ставит запятую вместо точки: «Понятно, какая площадь?» —
        # после первой части из одного слова («Понятно», «Конечно») смотрим и вторую.
        clauses = [c for c in _CLAUSE_BREAK.split(sentence) if self.norm.lemmas(c)]
        candidates = clauses[:1]
        if len(clauses) > 1 and len(self.norm.lemmas(clauses[0])) == 1:
            candidates.append(clauses[1])
        for clause in candidates:
            lemmas = self.norm.lemmas(clause)
            start = 0
            while start < len(lemmas) and lemmas[start] in _QUESTION_LEAD_INS:
                start += 1
            # Вопросительное слово сравнивается так же, как ключевые фразы (phrase_spans).
            if any(
                begin == start
                for word in question_words
                for begin, _end in self.norm.phrase_spans(clause, word)
            ):
                return True
        return False

    def questions(self, utterance: Utterance, question_words: tuple[str, ...]) -> list[str]:
        """Вопросы реплики. Последнее предложение без «?» тоже вопрос, если начинается
        с вопросительного слова и собеседник сразу ответил (whisper теряет «?»)."""
        sentences = [s.strip() for s in _SENTENCE.findall(utterance.text) if s.strip()]
        result = [s for s in sentences if s.endswith("?")]
        position = self.position.get(utterance.index)
        utterances = self.dialogue.utterances
        answered = (
            position is not None
            and position + 1 < len(utterances)
            and utterances[position + 1].role != utterance.role
        )
        if (
            answered
            and sentences
            and not sentences[-1].endswith("?")
            and self.is_open_question(sentences[-1], question_words)
        ):
            result.append(sentences[-1])
        return result

    def score_open_questions(self, item: ChecklistItem) -> ItemResult:
        question_words = self.expand(item.phrases, item)
        window = self.window(item)
        open_found: list[Utterance] = []
        closed_found: list[Utterance] = []
        for utterance in window:
            questions = self.questions(utterance, question_words)
            if not questions:
                continue
            if any(self.is_open_question(q, question_words) for q in questions):
                open_found.append(utterance)
            else:
                closed_found.append(utterance)
        where = self.window_label(item)
        if len(open_found) >= item.min_matches:
            return ItemResult(
                item_id=item.id,
                status="passed",
                reason=(
                    f"Открытых вопросов {where}: {len(open_found)} (нужно {item.min_matches})."
                ),
                evidence=tuple(_evidence(u) for u in open_found[:_MAX_EVIDENCE]),
            )
        reason = f"Открытых вопросов {where}: {len(open_found)}, нужно {item.min_matches}" + (
            f"; закрытых вопросов («да/нет»): {len(closed_found)}." if closed_found else "."
        )
        nearest = open_found + closed_found
        nearest.sort(key=lambda u: u.index)
        return ItemResult(
            item_id=item.id,
            status="failed",
            reason=reason,
            evidence=tuple(_evidence(u) for u in nearest[:_MAX_EVIDENCE]),
        )

    def score_conditional(self, item: ChecklistItem) -> ItemResult:
        triggers = self.expand(item.trigger, item)
        responses = self.expand(item.phrases, item)
        utterances = self.dialogue.utterances
        triggered = [
            pos
            for pos, u in enumerate(utterances)
            if u.role != item.speaker and self.matches(u, triggers, as_trigger=True)
        ]
        if not triggered:
            return ItemResult(
                item_id=item.id,
                status="na",
                reason="Условие не сработало: собеседник не произнёс фраз-триггеров "
                f"({_quote_list(item.trigger)}).",
                evidence=(),
            )
        handled: list[Evidence] = []
        for pos in triggered:
            trigger_utterance = utterances[pos]
            replies = list(
                islice(
                    (u for u in utterances[pos + 1 :] if u.role == item.speaker),
                    item.respond_within,
                )
            )
            answer = next((u for u in replies if self.matches(u, responses)), None)
            if answer is None:
                reply_word = "реплики" if item.respond_within > 1 else "реплика"
                reason = (
                    f"Возражение «{trigger_utterance.text}» без ответа по существу: "
                    f"в следующих {item.respond_within} {reply_word} "
                    f"{_ROLE_GENITIVE[item.speaker]} нет фраз отработки "
                    f"({_quote_list(item.phrases)})."
                )
                if not replies:
                    reason = (
                        f"Возражение «{trigger_utterance.text}» осталось без ответа "
                        f"{_ROLE_GENITIVE[item.speaker]}."
                    )
                return ItemResult(
                    item_id=item.id,
                    status="failed",
                    reason=reason,
                    evidence=(_evidence(trigger_utterance), *(_evidence(u) for u in replies)),
                )
            handled.extend((_evidence(trigger_utterance), _evidence(answer)))
        count = len(triggered)
        return ItemResult(
            item_id=item.id,
            status="passed",
            reason=f"Возражений: {count}, на каждое дан ответ по существу.",
            evidence=tuple(handled[: _MAX_EVIDENCE * 2]),
        )


def score_call(
    dialogue: Dialogue,
    checklist: Checklist,
    thresholds: Thresholds,
    normalizer: Normalizer | None = None,
) -> CallScore:
    """Проверяет звонок по каждому пункту чек-листа и считает итоговый балл 0–100."""
    scorer = _ItemScorer(dialogue, checklist, normalizer or _default_normalizer())
    results = tuple(scorer.score(item) for item in checklist.items)
    return summarize(results, checklist, thresholds)


def summarize(
    items: tuple[ItemResult, ...], checklist: Checklist, thresholds: Thresholds
) -> CallScore:
    """Итог по результатам пунктов: балл 0–100 (вес выполненных / вес применимых),
    этап потери — первый проваленный пункт, оценка по порогам."""
    weights = {item.id: item.weight for item in checklist.items}
    applicable = sum(weights.get(r.item_id, 0.0) for r in items if r.status != "na")
    passed = sum(weights.get(r.item_id, 0.0) for r in items if r.status == "passed")
    score = round(passed / applicable * 100, 1) if applicable else 0.0
    loss = next((r.item_id for r in items if r.status == "failed"), None)
    return CallScore(
        items=items, score=score, loss_item_id=loss, grade=grade_for(score, thresholds)
    )
