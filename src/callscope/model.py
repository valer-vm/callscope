"""Общие типы данных CallScope."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

Channel = Literal["left", "right"]
Role = Literal["manager", "client"]
ItemStatus = Literal["passed", "failed", "na"]
Grade = Literal["good", "medium", "bad"]


@dataclass(frozen=True)
class Segment:
    """Сырой сегмент расшифровки, до назначения ролей."""

    start: float
    end: float
    text: str
    channel: Channel


@dataclass(frozen=True)
class RawTranscript:
    segments: tuple[Segment, ...]
    mode: Literal["stereo", "mono"]
    duration: float

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "duration": self.duration,
            "segments": [
                {"start": s.start, "end": s.end, "text": s.text, "channel": s.channel}
                for s in self.segments
            ],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> RawTranscript:
        segments = tuple(
            Segment(
                start=float(s["start"]),
                end=float(s["end"]),
                text=str(s["text"]),
                channel=s["channel"],
            )
            for s in d["segments"]
        )
        return cls(
            segments=tuple(sorted(segments, key=lambda s: s.start)),
            mode=d["mode"],
            duration=float(d["duration"]),
        )


@dataclass(frozen=True)
class Utterance:
    index: int
    start: float
    end: float
    text: str
    role: Role


@dataclass(frozen=True)
class Dialogue:
    utterances: tuple[Utterance, ...]
    approximate: bool
    manager_channel: Channel
    role_source: Literal["intro", "first_speaker", "manual"]

    def to_json(self) -> dict[str, Any]:
        return {
            "approximate": self.approximate,
            "manager_channel": self.manager_channel,
            "role_source": self.role_source,
            "utterances": [
                {"index": u.index, "start": u.start, "end": u.end, "text": u.text, "role": u.role}
                for u in self.utterances
            ],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Dialogue:
        utterances = tuple(
            Utterance(
                index=int(u.get("index", i)),
                start=float(u["start"]),
                end=float(u["end"]),
                text=str(u["text"]),
                role=u["role"],
            )
            for i, u in enumerate(d["utterances"])
        )
        return cls(
            utterances=utterances,
            approximate=bool(d.get("approximate", False)),
            manager_channel=d.get("manager_channel", "left"),
            role_source=d.get("role_source", "manual"),
        )


@dataclass(frozen=True)
class CallMeta:
    file_name: str
    manager: str
    call_datetime: datetime | None
    matched_template: bool


@dataclass(frozen=True)
class Evidence:
    utterance_index: int
    start: float
    role: Role
    text: str


@dataclass(frozen=True)
class ItemResult:
    item_id: str
    status: ItemStatus
    reason: str
    evidence: tuple[Evidence, ...]
    by_model: bool = False


@dataclass(frozen=True)
class CallScore:
    items: tuple[ItemResult, ...]
    score: float
    loss_item_id: str | None
    grade: Grade


@dataclass(frozen=True)
class CallResult:
    meta: CallMeta
    dialogue: Dialogue
    score: CallScore
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class SkippedFile:
    file_name: str
    reason: str
