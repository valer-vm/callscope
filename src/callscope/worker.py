"""Фоновый воркер панели: один поток, записи по одной, переоценка — между записями.

whisper не рассчитан на параллельные расшифровки, поэтому поток один, а модель
создаётся лениво и живёт между задачами.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from callscope.asr import Transcriber
from callscope.cache import TranscriptCache
from callscope.checklist import Checklist
from callscope.config import AppConfig
from callscope.errors import AudioError, CallScopeError
from callscope.pipeline import analyze_raw, transcript_for
from callscope.rescoring import rescore_all
from callscope.storage import CallRecord, Storage
from callscope.textnorm import Normalizer

log = logging.getLogger("callscope")

CACHE_DIR_NAME = "cache"
_IDLE_WAIT = 1.0
_PROGRESS_STEP = 0.01


@dataclass(frozen=True)
class RescoreStatus:
    """Ход «переоценить всё»: idle / running (done из total) / done (в finished_at)."""

    state: Literal["idle", "running", "done"]
    done: int = 0
    total: int = 0
    finished_at: datetime | None = None
    error: str | None = None


class Worker:
    def __init__(
        self,
        storage: Storage,
        transcriber_factory: Callable[[AppConfig], Transcriber],
        load_settings: Callable[[], tuple[AppConfig, Checklist]],
    ) -> None:
        self.storage = storage
        self._factory = transcriber_factory
        self._load_settings = load_settings
        self._cache = TranscriptCache(storage.data_dir / CACHE_DIR_NAME)
        self._transcriber: Transcriber | None = None
        self._model_key: tuple[str, str | None] | None = None
        self._normalizer: Normalizer | None = None
        self._job_lock = threading.Lock()  # одна задача за раз (поток и run_once)
        self._state_lock = threading.Lock()  # флаг и ход переоценки; не ждёт расшифровку
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rescore_requested = False
        self._rescore = RescoreStatus("idle")

    # --- управление ---------------------------------------------------------------------

    def start(self) -> None:
        """Возвращает прерванные записи в очередь и запускает поток-демон."""
        if self._thread is not None and self._thread.is_alive():
            return
        count = self.storage.requeue_interrupted()
        if count:
            log.info("Возвращено в очередь после перезапуска: %d", count)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="callscope-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def wake(self) -> None:
        """Разбудить поток (новая запись в очереди)."""
        self._wake.set()

    def request_rescore(self) -> None:
        """Поставить «переоценить всё»; выполняется между записями."""
        with self._state_lock:
            self._rescore_requested = True
            self._rescore = RescoreStatus("running")
        self._wake.set()

    @property
    def rescore_status(self) -> RescoreStatus:
        with self._state_lock:
            return self._rescore

    def _set_rescore(self, status: RescoreStatus) -> None:
        with self._state_lock:
            self._rescore = status

    # --- работа -------------------------------------------------------------------------

    def run_once(self) -> bool:
        """Одна задача синхронно: переоценка (если запрошена) или одна запись из очереди.
        False — делать нечего."""
        with self._job_lock:
            with self._state_lock:
                rescore = self._rescore_requested
                self._rescore_requested = False
            if rescore:
                self._run_rescore()
                return True
            record = self.storage.next_queued()
            if record is None:
                return False
            self._process(record)
            return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.run_once()
            except Exception:  # воркер не должен умирать из-за одной задачи
                log.exception("Сбой фоновой обработки")
                busy = False
            if not busy:
                self._wake.wait(_IDLE_WAIT)
                self._wake.clear()

    def _process(self, record: CallRecord) -> None:
        try:
            config, checklist = self._load_settings()
            transcriber = self._transcriber_for(config)
            last = [0.0]

            def on_progress(fraction: float) -> None:
                if fraction - last[0] >= _PROGRESS_STEP or fraction >= 1.0:
                    last[0] = fraction
                    self.storage.set_progress(record.id, fraction)

            raw, _from_cache = transcript_for(
                record.audio_path, transcriber, self._cache, config, on_progress
            )
            result = analyze_raw(
                record.file_name,
                raw,
                config,
                checklist,
                self._normalizer_instance(),
                manager_channel=record.manager_channel,
                use_semantic=config.semantic.enabled,
            )
            self.storage.save_result(record.id, raw, result, checklist, config.thresholds)
        except AudioError as exc:
            # На диске запись лежит под служебным именем — в статусе нужно исходное.
            message = exc.message_ru.replace(record.audio_path.name, record.file_name)
            self.storage.save_error(record.id, message)
        except CallScopeError as exc:  # модель не загрузилась, настройки с ошибкой
            log.error("Запись %s не обработана: %s", record.file_name, exc.message_ru)
            self.storage.save_error(record.id, exc.message_ru)
        except Exception:
            log.exception("Сбой обработки записи %s", record.file_name)
            self.storage.save_error(
                record.id, "Внутренняя ошибка при обработке записи — подробности в журнале."
            )

    def _run_rescore(self) -> None:
        def on_progress(done: int, total: int) -> None:
            self._set_rescore(RescoreStatus("running", done, total))

        try:
            config, checklist = self._load_settings()
            total = len([r for r in self.storage.list_calls() if r.status == "done"])
            self._set_rescore(RescoreStatus("running", 0, total))
            rescore_all(self.storage, config, checklist, self._normalizer_instance(), on_progress)
            error = None
        except CallScopeError as exc:
            log.error("Переоценка не выполнена: %s", exc.message_ru)
            error = exc.message_ru
        except Exception:  # любой сбой переоценки — в статус, воркер работает дальше
            log.exception("Сбой переоценки")
            error = "Переоценка прервана внутренней ошибкой — подробности в журнале."
        with self._state_lock:
            if self._rescore_requested:
                # Пока шёл этот проход, переоценку запросили снова (например, после новой
                # правки чек-листа): «готово» показывать рано — следующий проход впереди.
                self._rescore = RescoreStatus("running", 0, self._rescore.total)
            else:
                current = self._rescore
                self._rescore = RescoreStatus(
                    "done", current.done, current.total, datetime.now(UTC), error
                )

    def _transcriber_for(self, config: AppConfig) -> Transcriber:
        """Модель создаётся лениво один раз; заново — только если сменили модель в настройках."""
        key = (config.model_size, config.model_path)
        if self._transcriber is None or self._model_key != key:
            self._transcriber = self._factory(config)
            self._model_key = key
        return self._transcriber

    def _normalizer_instance(self) -> Normalizer:
        if self._normalizer is None:
            self._normalizer = Normalizer()
        return self._normalizer
