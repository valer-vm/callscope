"""Назначение ролей «Менеджер» / «Клиент» сторонам разговора."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from callscope.config import AppConfig
from callscope.model import Channel, Dialogue, RawTranscript, Segment, Utterance
from callscope.textnorm import Normalizer

_SIDE_NAMES: dict[Channel, str] = {"left": "левый канал", "right": "правый канал"}
_NON_LETTERS = re.compile(r"[^0-9a-zа-я]+")


@lru_cache(maxsize=1)
def _normalizer() -> Normalizer:
    return Normalizer()


def _merge(segments: tuple[Segment, ...]) -> list[Segment]:
    """Склеивает подряд идущие сегменты одной стороны в одну реплику."""
    merged: list[Segment] = []
    for segment in sorted(segments, key=lambda s: s.start):
        text = segment.text.strip()
        if not text:
            continue
        if merged and merged[-1].channel == segment.channel:
            last = merged[-1]
            merged[-1] = Segment(
                start=last.start,
                end=max(last.end, segment.end),
                text=f"{last.text} {text}",
                channel=last.channel,
            )
        else:
            merged.append(Segment(segment.start, segment.end, text, segment.channel))
    return merged


def _squash(text: str) -> str:
    """Нижний регистр, ё→е, без пробелов и знаков: «Строй Дом» == «стройдом»."""
    return _NON_LETTERS.sub("", text.lower().replace("ё", "е"))


def _introduces(text: str, config: AppConfig) -> bool:
    normalizer = _normalizer()
    if any(normalizer.contains_phrase(text, phrase) for phrase in config.intro_phrases):
        return True
    squashed = _squash(text)
    return any(_squash(name) and _squash(name) in squashed for name in config.company_names)


def _manual_channel(
    config: AppConfig, file_name: str, cli_channel: Literal["auto", "left", "right"] | None
) -> Channel | None:
    if cli_channel in ("left", "right"):
        return cli_channel
    if file_name in config.manager_channel_overrides:
        return config.manager_channel_overrides[file_name]
    if config.manager_channel in ("left", "right"):
        return config.manager_channel
    return None


def assign_roles(
    raw: RawTranscript,
    config: AppConfig,
    file_name: str,
    cli_channel: Literal["auto", "left", "right"] | None = None,
) -> tuple[Dialogue, list[str]]:
    """Определяет сторону менеджера и собирает диалог из реплик.

    Приоритет: cli_channel (не auto) → manager_channel_overrides[file_name] →
    config.manager_channel (не auto) → сторона, первой представившаяся от компании →
    первый заговоривший (с предупреждением).
    """
    turns = _merge(raw.segments)
    warnings: list[str] = []
    role_source: Literal["intro", "first_speaker", "manual"]

    if not turns:
        warnings.append(f"{file_name}: в записи не распознано ни одной реплики.")

    manual = _manual_channel(config, file_name, cli_channel)
    if manual is not None:
        manager_channel: Channel = manual
        role_source = "manual"
    elif not turns:
        manager_channel = "left"
        role_source = "first_speaker"
    else:
        intro = next((t for t in turns if _introduces(t.text, config)), None)
        if intro is not None:
            manager_channel = intro.channel
            role_source = "intro"
        else:
            manager_channel = turns[0].channel
            role_source = "first_speaker"
            warnings.append(
                f"{file_name}: никто не представился от имени компании — менеджером "
                f"считается тот, кто заговорил первым ({_SIDE_NAMES[manager_channel]})."
            )

    utterances = tuple(
        Utterance(
            index=i,
            start=t.start,
            end=t.end,
            text=t.text,
            role="manager" if t.channel == manager_channel else "client",
        )
        for i, t in enumerate(turns)
    )
    dialogue = Dialogue(
        utterances=utterances,
        approximate=raw.mode == "mono",
        manager_channel=manager_channel,
        role_source=role_source,
    )
    return dialogue, warnings
