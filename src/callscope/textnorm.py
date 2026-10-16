"""Нормализация русского текста: леммы pymorphy3 и поиск фраз по леммам."""

from __future__ import annotations

import re
from functools import lru_cache

import pymorphy3

_WORD = re.compile(r"[0-9a-zа-я]+(?:-[0-9a-zа-я]+)*")

# Видовые пары глаголов: совершенный вид → несовершенный, чтобы «предложу» и
# «предлагаю» сводились к одной лемме. Список — частые глаголы разговора продаж.
_ASPECT_PAIRS: dict[str, str] = {
    "предложить": "предлагать",
    "рассказать": "рассказывать",
    "оформить": "оформлять",
    "отправить": "отправлять",
    "выслать": "высылать",
    "прислать": "присылать",
    "записать": "записывать",
    "перезвонить": "перезванивать",
    "позвонить": "звонить",
    "договориться": "договариваться",
    "уточнить": "уточнять",
    "подобрать": "подбирать",
    "рассчитать": "рассчитывать",
    "посчитать": "считать",
    "подсказать": "подсказывать",
    "показать": "показывать",
    "согласовать": "согласовывать",
    "назначить": "назначать",
    "обсудить": "обсуждать",
    "ответить": "отвечать",
    "сделать": "делать",
    "решить": "решать",
    "помочь": "помогать",
    "приехать": "приезжать",
    "доставить": "доставлять",
    "выбрать": "выбирать",
    "спросить": "спрашивать",
    "узнать": "узнавать",
    "понять": "понимать",
    "встретиться": "встречаться",
    "получить": "получать",
    "заказать": "заказывать",
    "посмотреть": "смотреть",
    "подготовить": "готовить",
    "сэкономить": "экономить",
    "объяснить": "объяснять",
    "дать": "давать",
    "забронировать": "бронировать",
    "зафиксировать": "фиксировать",
    "предоставить": "предоставлять",
    "замерить": "замерять",
}


# Слова, у которых pymorphy даёт разные леммы для форм одного слова: сводим к общей,
# чтобы сравнение по самым вероятным разборам не теряло «здравствуйте/здравствуй».
_LEMMA_ALIASES: dict[str, str] = {
    "здравствуйте": "здравствовать",
    "здравствуй": "здравствовать",
}


def _canonical(lemma: str) -> str:
    lemma = _LEMMA_ALIASES.get(lemma, lemma)
    return _ASPECT_PAIRS.get(lemma, lemma)


def _clean(text: str) -> str:
    return text.lower().replace("ё", "е")


class Normalizer:
    """Приводит слова к начальной форме. Анализатор pymorphy3 и разборы кэшируются."""

    def __init__(self) -> None:
        self._morph = pymorphy3.MorphAnalyzer()
        self._word_lemmas = lru_cache(maxsize=50_000)(self._compute_word_lemmas)

    def _compute_word_lemmas(self, word: str) -> tuple[str, ...]:
        """Все возможные леммы слова; первая — самая вероятная."""
        result: list[str] = []
        for parse in self._morph.parse(word):
            lemma = _canonical(_clean(parse.normal_form))
            if lemma not in result:
                result.append(lemma)
        return tuple(result) or (word,)

    def lemmas(self, text: str) -> list[str]:
        """Леммы слов текста: нижний регистр, ё→е, без пунктуации."""
        return [variants[0] for _, variants in self._words(text)]

    def phrase_spans(self, text: str, phrase: str) -> list[tuple[int, int]]:
        """Позиции [начало, конец) фразы среди слов текста (индексы как в lemmas).

        Слово фразы совпадает со словом текста, если самая вероятная лемма одного
        входит в разборы другого: «дорогое» подходит к «дорого», а «дорога» — нет
        (у неё «дорогой» лишь редкий разбор, а у «дорого» — не основной).
        """
        needle = [
            (self._primary_lemma(word, variants), set(variants))
            for word, variants in self._words(phrase)
        ]
        if not needle:
            return []
        haystack = [
            (self._primary_lemma(word, variants), set(variants))
            for word, variants in self._words(text)
        ]
        size = len(needle)
        return [
            (start, start + size)
            for start in range(len(haystack) - size + 1)
            if all(
                needle[i][0] in haystack[start + i][1] or haystack[start + i][0] in needle[i][1]
                for i in range(size)
            )
        ]

    def _words(self, text: str) -> list[tuple[str, tuple[str, ...]]]:
        return [(word, self._word_lemmas(word)) for word in _WORD.findall(_clean(text))]

    @staticmethod
    def _primary_lemma(word: str, variants: tuple[str, ...]) -> str:
        """Самая вероятная лемма; слово, само стоящее в одной из начальных форм, берёт
        её: «дорогой» — прилагательное, а не «дорога» в творительном."""
        own = _canonical(word)
        return own if own in variants else variants[0]

    def contains_phrase(self, text: str, phrase: str) -> bool:
        """True, если леммы фразы идут подряд среди лемм текста."""
        return bool(self.phrase_spans(text, phrase))
