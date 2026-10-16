"""Хранилище панели: SQLite (`callscope.db`, WAL) и файлы записей в папке данных.

Модуль владеет схемой — другие модули SQL не пишут. Каждая операция открывает своё
соединение, поэтому хранилищем одновременно пользуются фоновый воркер и запросы панели.
Время загрузки и правок хранится в UTC ISO; дата звонка — как разобрана из имени файла.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from callscope import stats
from callscope.checklist import CHECKLIST_FILE_NAME, Checklist, ChecklistItem, load_checklist
from callscope.config import CONFIG_FILE_NAME, DEFAULTS_DIR, Thresholds, load_config
from callscope.errors import CallScopeError, ConfigError
from callscope.filenames import UNDEFINED_MANAGER, parse_filename
from callscope.model import (
    CallMeta,
    CallResult,
    CallScore,
    Dialogue,
    Evidence,
    ItemResult,
    ItemStatus,
    RawTranscript,
)
from callscope.scoring import summarize
from callscope.stats import CallFilters

log = logging.getLogger("callscope")

CallStatus = Literal["queued", "processing", "done", "error"]
ManagerChannelChoice = Literal["auto", "left", "right"]

DB_FILE_NAME = "callscope.db"
AUDIO_DIR_NAME = "audio"
SCHEMA_VERSION = 1
_CHUNK = 1 << 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    audio_name TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    error TEXT,
    manager TEXT NOT NULL,
    call_datetime TEXT,
    matched_template INTEGER NOT NULL DEFAULT 0,
    manager_channel TEXT NOT NULL DEFAULT 'auto',
    raw_json TEXT,
    dialogue_json TEXT,
    auto_items_json TEXT,
    score_json TEXT,
    score REAL,
    basis_json TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS calls_status ON calls (status, uploaded_at, id);
CREATE TABLE IF NOT EXISTS corrections (
    call_id INTEGER NOT NULL REFERENCES calls (id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    status TEXT,
    comment TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (call_id, item_id)
);
"""

# Колонки для списков: без сырой расшифровки и диалога.
_LIGHT = (
    "id, sha256, file_name, audio_name, uploaded_at, status, progress, error, manager, "
    "call_datetime, matched_template, manager_channel, auto_items_json, score_json, "
    "warnings_json"
)
_FULL = _LIGHT + ", raw_json, dialogue_json"


@dataclass(frozen=True)
class Correction:
    """Ручная правка пункта. `status=None` — только комментарий, оценка автоматическая."""

    item_id: str
    status: ItemStatus | None
    comment: str
    updated_at: datetime


@dataclass(frozen=True)
class CallRecord:
    id: int
    sha256: str
    file_name: str
    audio_path: Path
    uploaded_at: datetime
    status: CallStatus
    progress: float
    error: str | None
    manager: str
    call_datetime: datetime | None
    matched_template: bool
    manager_channel: ManagerChannelChoice
    raw: RawTranscript | None
    dialogue: Dialogue | None
    auto_items: tuple[ItemResult, ...]
    corrections: dict[str, Correction]
    score: CallScore | None  # действующая оценка (с правками)
    warnings: tuple[str, ...]


# --- (де)сериализация ---------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _item_to_json(item: ItemResult) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "status": item.status,
        "reason": item.reason,
        "by_model": item.by_model,
        "evidence": [
            {"utterance_index": e.utterance_index, "start": e.start, "role": e.role, "text": e.text}
            for e in item.evidence
        ],
    }


def _item_from_json(d: dict[str, Any]) -> ItemResult:
    return ItemResult(
        item_id=d["item_id"],
        status=d["status"],
        reason=d["reason"],
        evidence=tuple(
            Evidence(int(e["utterance_index"]), float(e["start"]), e["role"], e["text"])
            for e in d["evidence"]
        ),
        by_model=bool(d.get("by_model", False)),
    )


def _items_to_json(items: tuple[ItemResult, ...]) -> str:
    return json.dumps([_item_to_json(i) for i in items], ensure_ascii=False)


def _score_to_json(score: CallScore) -> str:
    return json.dumps(
        {
            "items": [_item_to_json(i) for i in score.items],
            "score": score.score,
            "loss_item_id": score.loss_item_id,
            "grade": score.grade,
        },
        ensure_ascii=False,
    )


def _score_from_json(text: str) -> CallScore:
    d = json.loads(text)
    return CallScore(
        items=tuple(_item_from_json(i) for i in d["items"]),
        score=float(d["score"]),
        loss_item_id=d["loss_item_id"],
        grade=d["grade"],
    )


def _safe_suffix(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    return suffix if suffix[1:].isalnum() else ""


# --- хранилище ------------------------------------------------------------------------


class Storage:
    """Звонки, очередь и правки в `<data_dir>/callscope.db`, записи в `<data_dir>/audio/`."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.audio_dir = self.data_dir / AUDIO_DIR_NAME
        self.checklist_path = self.data_dir / CHECKLIST_FILE_NAME
        self.config_path = self.data_dir / CONFIG_FILE_NAME
        self.db_path = self.data_dir / DB_FILE_NAME
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        for target, name in (
            (self.checklist_path, CHECKLIST_FILE_NAME),
            (self.config_path, CONFIG_FILE_NAME),
        ):
            if not target.exists():
                shutil.copyfile(DEFAULTS_DIR / name, target)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
            columns = {r["name"] for r in db.execute("PRAGMA table_info(calls)")}
            if "basis_json" not in columns:  # база, созданная до появления колонки
                db.execute("ALTER TABLE calls ADD COLUMN basis_json TEXT")
            db.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Своё соединение на операцию; транзакция фиксируется при выходе без ошибки."""
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
            yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    # --- приём ----------------------------------------------------------------------

    def add_upload(self, file_name: str, content: bytes | Path) -> tuple[CallRecord, bool]:
        """Кладёт запись в `audio/<sha256>.<расш>` и ставит в очередь.
        То же содержимое уже есть — возвращает существующую запись и False."""
        file_name = Path(file_name).name or file_name
        fd, tmp_name = tempfile.mkstemp(dir=self.audio_dir, prefix=".upload-", suffix=".tmp")
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as out:
                if isinstance(content, bytes | bytearray):
                    digest.update(content)
                    out.write(content)
                else:
                    with Path(content).open("rb") as source:
                        while chunk := source.read(_CHUNK):
                            digest.update(chunk)
                            out.write(chunk)
            sha = digest.hexdigest()
            existing = self._by_sha(sha)
            if existing is not None:
                return existing, False
            audio_name = f"{sha}{_safe_suffix(file_name)}"
            os.replace(tmp_name, self.audio_dir / audio_name)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)

        meta = self._parse_meta(file_name)
        try:
            with self._transaction() as db:
                cursor = db.execute(
                    "INSERT INTO calls (sha256, file_name, audio_name, uploaded_at, manager, "
                    "call_datetime, matched_template) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        sha,
                        file_name,
                        audio_name,
                        _iso(_now()),
                        meta.manager,
                        _iso(meta.call_datetime) if meta.call_datetime else None,
                        int(meta.matched_template),
                    ),
                )
                call_id = int(cursor.lastrowid or 0)
        except sqlite3.IntegrityError:  # тот же файл загрузили одновременно
            existing = self._by_sha(sha)
            if existing is None:
                raise
            return existing, False
        record = self.get(call_id)
        assert record is not None
        return record, True

    def _parse_meta(self, file_name: str) -> CallMeta:
        try:
            config = load_config(self.config_path)
        except ConfigError as exc:
            log.warning("Имя файла не разобрано: %s", exc.message_ru)
            return CallMeta(file_name, UNDEFINED_MANAGER, None, False)
        return parse_filename(
            file_name, config.filename_template, config.date_format, config.time_format
        )

    def _by_sha(self, sha: str) -> CallRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT id FROM calls WHERE sha256 = ?", (sha,)).fetchone()
        return self.get(int(row["id"])) if row else None

    # --- очередь ----------------------------------------------------------------------

    def next_queued(self) -> CallRecord | None:
        """Самая давняя запись в очереди переводится в «обрабатывается» (атомарно)."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT id FROM calls WHERE status = 'queued' ORDER BY uploaded_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE calls SET status = 'processing', progress = 0, error = NULL "
                "WHERE id = ?",
                (row["id"],),
            )
        return self.get(int(row["id"]))

    def requeue_interrupted(self) -> int:
        """«Обрабатывается» → «в очереди» (после перезапуска). Возвращает число записей."""
        with self._transaction() as db:
            cursor = db.execute(
                "UPDATE calls SET status = 'queued', progress = 0 WHERE status = 'processing'"
            )
            return cursor.rowcount

    def set_progress(self, call_id: int, fraction: float) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE calls SET progress = ? WHERE id = ? AND status = 'processing'",
                (min(max(float(fraction), 0.0), 1.0), call_id),
            )

    def save_result(
        self,
        call_id: int,
        raw: RawTranscript,
        result: CallResult,
        checklist: Checklist | None = None,
        thresholds: Thresholds | None = None,
    ) -> None:
        """Расшифровка и оценка записи → «готово». `checklist`/`thresholds` — по которым
        посчитана оценка (не переданы — рабочие файлы); их веса хранятся вместе с оценкой."""
        basis = self._basis(checklist, thresholds)
        with self._transaction() as db:
            if not self._exists(db, call_id):
                return  # запись удалили во время обработки
            db.execute(
                "UPDATE calls SET raw_json = ?, manager = ?, call_datetime = ?, "
                "matched_template = ? WHERE id = ?",
                (
                    json.dumps(raw.to_json(), ensure_ascii=False),
                    result.meta.manager,
                    _iso(result.meta.call_datetime) if result.meta.call_datetime else None,
                    int(result.meta.matched_template),
                    call_id,
                ),
            )
            self._write_analysis(db, call_id, result, basis)
            db.execute(
                "UPDATE calls SET status = 'done', progress = 1, error = NULL WHERE id = ?",
                (call_id,),
            )

    def save_error(self, call_id: int, message: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE calls SET status = 'error', progress = 0, error = ? WHERE id = ?",
                (message, call_id),
            )

    def retry(self, call_id: int) -> None:
        """«Ошибка» → «в очереди»; в другом статусе ничего не меняет."""
        with self._connect() as db:
            db.execute(
                "UPDATE calls SET status = 'queued', progress = 0, error = NULL "
                "WHERE id = ? AND status = 'error'",
                (call_id,),
            )

    # --- чтение -----------------------------------------------------------------------

    def get(self, call_id: int) -> CallRecord | None:
        with self._connect() as db:
            row = db.execute(f"SELECT {_FULL} FROM calls WHERE id = ?", (call_id,)).fetchone()
            if row is None:
                return None
            corrections = self._corrections(db, [call_id])
        return self._record(row, corrections.get(call_id, {}), full=True)

    def list_calls(self, filters: CallFilters | None = None) -> list[CallRecord]:
        """Звонки без сырой расшифровки и диалога, новые по дате звонка — сверху.

        Без фильтров — все записи в любом статусе. С `stats.CallFilters` — только «готово»,
        отобранные `stats.apply_filters` по действующей оценке.
        """
        with self._connect() as db:
            sql = f"SELECT {_LIGHT} FROM calls"
            if filters is not None:
                sql += " WHERE status = 'done'"
            rows = db.execute(
                sql + " ORDER BY call_datetime IS NULL, call_datetime DESC, uploaded_at DESC, "
                "id DESC"
            ).fetchall()
            corrections = self._corrections(db, None)
        records = [self._record(r, corrections.get(int(r["id"]), {})) for r in rows]
        if filters is None:
            return records
        return stats.apply_filters(records, filters)

    def queue_overview(self, limit: int = 200) -> list[CallRecord]:
        """Последние загрузки (новые сверху) для списка на странице «Загрузка»."""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {_LIGHT} FROM calls ORDER BY uploaded_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            corrections = self._corrections(db, ids)
        return [self._record(r, corrections.get(int(r["id"]), {})) for r in rows]

    # --- правки и изменения -------------------------------------------------------------

    def set_correction(
        self, call_id: int, item_id: str, status: ItemStatus | None, comment: str
    ) -> CallRecord:
        """Ручная правка пункта; балл и этап потери пересчитываются по действующим оценкам."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT auto_items_json FROM calls WHERE id = ? AND status = 'done'", (call_id,)
            ).fetchone()
            if row is None:
                raise CallScopeError("Звонок не найден или ещё не обработан.")
            auto_ids = {d["item_id"] for d in json.loads(row["auto_items_json"] or "[]")}
            if item_id not in auto_ids:
                raise CallScopeError(f"В оценке звонка нет пункта «{item_id}».")
            db.execute(
                "INSERT INTO corrections (call_id, item_id, status, comment, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (call_id, item_id) DO UPDATE SET "
                "status = excluded.status, comment = excluded.comment, "
                "updated_at = excluded.updated_at",
                (call_id, item_id, status, comment, _iso(_now())),
            )
            self._refresh_score(db, call_id)
        record = self.get(call_id)
        assert record is not None
        return record

    def clear_correction(self, call_id: int, item_id: str) -> CallRecord:
        """Вернуть автоматическую оценку пункта."""
        with self._transaction() as db:
            if not self._exists(db, call_id):
                raise CallScopeError("Звонок не найден.")
            db.execute(
                "DELETE FROM corrections WHERE call_id = ? AND item_id = ?", (call_id, item_id)
            )
            self._refresh_score(db, call_id)
        record = self.get(call_id)
        assert record is not None
        return record

    def set_manager_channel(self, call_id: int, channel: ManagerChannelChoice) -> None:
        with self._connect() as db:
            db.execute("UPDATE calls SET manager_channel = ? WHERE id = ?", (channel, call_id))

    def update_analysis(
        self,
        call_id: int,
        result: CallResult,
        checklist: Checklist | None = None,
        thresholds: Thresholds | None = None,
    ) -> None:
        """Новая авто-оценка (переоценка, смена сторон). Менеджер и дата не меняются
        (это `update_meta`); правки сохраняются, правки отсутствующих пунктов удаляются.
        `checklist`/`thresholds` — как в `save_result`."""
        basis = self._basis(checklist, thresholds)
        with self._transaction() as db:
            if self._exists(db, call_id):
                self._write_analysis(db, call_id, result, basis)

    def update_meta(self, call_id: int, meta: CallMeta) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE calls SET manager = ?, call_datetime = ?, matched_template = ? "
                "WHERE id = ?",
                (
                    meta.manager,
                    _iso(meta.call_datetime) if meta.call_datetime else None,
                    int(meta.matched_template),
                    call_id,
                ),
            )

    def delete(self, call_id: int) -> None:
        """Удаляет звонок, его правки и файл записи. Без корзины."""
        with self._transaction() as db:
            row = db.execute("SELECT audio_name FROM calls WHERE id = ?", (call_id,)).fetchone()
            if row is None:
                return
            db.execute("DELETE FROM calls WHERE id = ?", (call_id,))
        with contextlib.suppress(FileNotFoundError):
            (self.audio_dir / row["audio_name"]).unlink()

    # --- внутреннее ---------------------------------------------------------------------

    @staticmethod
    def _exists(db: sqlite3.Connection, call_id: int) -> bool:
        return db.execute("SELECT 1 FROM calls WHERE id = ?", (call_id,)).fetchone() is not None

    def _basis(self, checklist: Checklist | None, thresholds: Thresholds | None) -> str:
        """Веса пунктов и пороги, по которым посчитана авто-оценка (JSON для колонки)."""
        if checklist is None:
            checklist = load_checklist(self.checklist_path)
        if thresholds is None:
            thresholds = load_config(self.config_path).thresholds
        return json.dumps(
            {
                "items": [
                    {"id": i.id, "title": i.title, "weight": i.weight} for i in checklist.items
                ],
                "good": thresholds.good,
                "medium": thresholds.medium,
            },
            ensure_ascii=False,
        )

    def _write_analysis(
        self, db: sqlite3.Connection, call_id: int, result: CallResult, basis: str
    ) -> None:
        item_ids = [i.item_id for i in result.score.items]
        placeholders = ",".join("?" * len(item_ids))
        db.execute(
            f"DELETE FROM corrections WHERE call_id = ? AND item_id NOT IN ({placeholders})",
            (call_id, *item_ids),
        )
        db.execute(
            "UPDATE calls SET dialogue_json = ?, auto_items_json = ?, warnings_json = ?, "
            "basis_json = ? WHERE id = ?",
            (
                json.dumps(result.dialogue.to_json(), ensure_ascii=False),
                _items_to_json(result.score.items),
                json.dumps(list(result.warnings), ensure_ascii=False),
                basis,
                call_id,
            ),
        )
        self._refresh_score(db, call_id, auto_score=result.score)

    def _refresh_score(
        self, db: sqlite3.Connection, call_id: int, auto_score: CallScore | None = None
    ) -> None:
        """Пересчитывает действующую оценку: правка, если есть, иначе авто."""
        row = db.execute(
            "SELECT auto_items_json, basis_json FROM calls WHERE id = ?", (call_id,)
        ).fetchone()
        auto_items = tuple(_item_from_json(d) for d in json.loads(row["auto_items_json"] or "[]"))
        corrections = self._corrections(db, [call_id]).get(call_id, {})
        overrides = {k: c.status for k, c in corrections.items() if c.status is not None}
        if not overrides and auto_score is not None:
            score = auto_score
        else:
            items = tuple(
                replace(i, status=overrides[i.item_id]) if i.item_id in overrides else i
                for i in auto_items
            )
            checklist, thresholds = _basis_from_json(row["basis_json"] or self._basis(None, None))
            weighted = {i.id for i in checklist.items}
            missing = [i.item_id for i in items if i.item_id not in weighted]
            if missing:
                raise CallScopeError(
                    f"Нет веса для пунктов {', '.join(missing)} — переоцените звонки."
                )
            score = summarize(items, checklist, thresholds)
        db.execute(
            "UPDATE calls SET score_json = ?, score = ? WHERE id = ?",
            (_score_to_json(score), score.score, call_id),
        )

    @staticmethod
    def _corrections(
        db: sqlite3.Connection, call_ids: list[int] | None
    ) -> dict[int, dict[str, Correction]]:
        sql = "SELECT call_id, item_id, status, comment, updated_at FROM corrections"
        params: tuple[int, ...] = ()
        if call_ids is not None:
            if not call_ids:
                return {}
            sql += f" WHERE call_id IN ({','.join('?' * len(call_ids))})"
            params = tuple(call_ids)
        result: dict[int, dict[str, Correction]] = {}
        for row in db.execute(sql, params):
            result.setdefault(int(row["call_id"]), {})[row["item_id"]] = Correction(
                item_id=row["item_id"],
                status=row["status"],
                comment=row["comment"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
        return result

    def _record(
        self, row: sqlite3.Row, corrections: dict[str, Correction], full: bool = False
    ) -> CallRecord:
        raw = dialogue = None
        if full:
            if row["raw_json"]:
                raw = RawTranscript.from_json(json.loads(row["raw_json"]))
            if row["dialogue_json"]:
                dialogue = Dialogue.from_json(json.loads(row["dialogue_json"]))
        return CallRecord(
            id=int(row["id"]),
            sha256=row["sha256"],
            file_name=row["file_name"],
            audio_path=self.audio_dir / row["audio_name"],
            uploaded_at=datetime.fromisoformat(row["uploaded_at"]),
            status=row["status"],
            progress=float(row["progress"]),
            error=row["error"],
            manager=row["manager"],
            call_datetime=(
                datetime.fromisoformat(row["call_datetime"]) if row["call_datetime"] else None
            ),
            matched_template=bool(row["matched_template"]),
            manager_channel=row["manager_channel"],
            raw=raw,
            dialogue=dialogue,
            auto_items=tuple(
                _item_from_json(d) for d in json.loads(row["auto_items_json"] or "[]")
            ),
            corrections=corrections,
            score=_score_from_json(row["score_json"]) if row["score_json"] else None,
            warnings=tuple(json.loads(row["warnings_json"] or "[]")),
        )


def _basis_from_json(text: str) -> tuple[Checklist, Thresholds]:
    d = json.loads(text)
    items = tuple(
        ChecklistItem(id=i["id"], title=i["title"], weight=i["weight"]) for i in d["items"]
    )
    return Checklist(items=items), Thresholds(good=d["good"], medium=d["medium"])
