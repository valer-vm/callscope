"""Кэш сырых расшифровок: повторный запуск не расшифровывает уже обработанные файлы."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path

from callscope.errors import AudioError
from callscope.model import RawTranscript

# Меняется, когда меняется способ расшифровки или формат записи кэша.
_FORMAT_VERSION = "2"  # 2 — моно делится по голосу
_CHUNK = 1 << 20


class TranscriptCache:
    """JSON-файлы сырых расшифровок в папке (обычно `.callscope-cache/`)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def key(self, audio: Path, model_size: str, mono_turn_gap: float) -> str:
        """sha256 содержимого файла + модель + порог пауз для моно + версия формата.
        Имя файла и чек-лист не влияют."""
        digest = hashlib.sha256()
        try:
            with audio.open("rb") as handle:
                while chunk := handle.read(_CHUNK):
                    digest.update(chunk)
        except OSError:
            raise AudioError(f"Не удалось прочитать файл {audio.name}.") from None
        meta = f"|model={model_size}|gap={mono_turn_gap!r}|format={_FORMAT_VERSION}".encode()
        return hashlib.sha256(digest.digest() + meta).hexdigest()

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> RawTranscript | None:
        """Расшифровка из кэша; нет записи или она битая — None (промах)."""
        try:
            data = json.loads(self._path(key).read_text(encoding="utf-8"))
            return RawTranscript.from_json(data)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def put(self, key: str, raw: RawTranscript) -> None:
        """Атомарная запись: временный файл в той же папке, затем os.replace."""
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(raw.to_json(), ensure_ascii=False)
        fd, tmp_name = tempfile.mkstemp(dir=self.directory, prefix=f".{key}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._path(key))
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
