# Архитектура CallScope

## Поток данных

`аудио → RawTranscript → Dialogue → CallScore → CallResult → Summary → HTML/XLSX`

| Шаг | Модуль | Вход → выход |
|---|---|---|
| Имя файла | `filenames.parse_filename` | имя → `CallMeta` (не подошло шаблону: менеджер «Не определён») |
| Расшифровка | `asr.Transcriber` (+ `cache.TranscriptCache`) | аудио → `RawTranscript` (сегменты со стороной left/right) |
| Роли | `roles.assign_roles` | `RawTranscript` → `Dialogue` (реплики manager/client) + предупреждения |
| Оценка | `scoring.score_call` | `Dialogue` + `Checklist` → `CallScore` |
| Перепроверка | `semantic.recheck` | failed-пункты и `na` с условием (`trigger`), через Ollama; вызывают `pipeline.analyze_raw` при `semantic.enabled` (CLI, очередь, переоценка) |
| Сводка | `aggregate.build_summary` | `CallResult[]` → `Summary` |
| Отчёты | `report_html.write_html`, `report_xlsx.write_xlsx` | читают только `Summary` |

Оркестрация — `cli.analyze` (`src/callscope/cli.py`).

## Границы модулей

- `model.py` владеет всеми общими типами; остальные модули их импортируют, не определяют свои.
- `faster-whisper` виден только в `asr.py`; Ollama — только в `semantic.py`.
- `config.py` / `checklist.py` читают YAML; встроенные по умолчанию — `src/callscope/defaults/`.
- `textnorm.Normalizer` — единственное место морфологии (pymorphy3); `scoring` и `roles` ходят через него.
- Отчёты не знают про аудио и оценку: только `Summary`.

## Соглашения

- Сообщения пользователю — по-русски, имена в коде — по-английски.
- Данные — `dataclass(frozen=True)` с аннотациями типов.
- Ошибка конфига — `errors.ConfigError` (CLI печатает `message_ru`, выход 2); нечитаемый файл — `AudioError` (файл пропускается); сбой загрузки модели — `CallScopeError` (останавливает запуск).
- Коды выхода CLI: 0 ок, 1 нечего/не удалось обработать, 2 ошибка ввода или конфига.
- Сети нет, кроме скачивания модели whisper и Ollama на localhost при `semantic.enabled`; нелокальный url — предупреждение без запросов. Исключение — адрес из `CALLSCOPE_OLLAMA_URL` (задаёт администратор; в Docker — соседний контейнер `ollama`).
- Тесты не требуют модели, аудио и сети.
- Оценка без аудио: диалог из `tests/fixtures/*.json` через `Dialogue.from_json`.
- Балл = сумма весов passed / сумма весов (passed+failed) × 100; `na` не считается; потеря звонка — первый failed по порядку чек-листа.
- Роли: приоритет `--manager-channel` → `manager_channel_overrides` → `manager_channel` в конфиге → интро/название компании → первый заговоривший (с предупреждением).
- Моно: стороны различаются по голосу (`diarize.py`: огибающая спектра + высота тона, k-means на две группы); если голоса не разделились — по паузам (`mono_turn_gap`) и «?». Диалог помечен `approximate`. Пороги `diarize` подобраны на тестовых звонках, сведённых в моно (один голос — разница огибающих 0,7–1,3, разные — от 1,2).
- Кэш расшифровок — `<папка звонков>/.callscope-cache/`; ключ = sha256 файла + модель + версия формата; запись атомарная (tmp + `os.replace`).
- Решения с причинами — `docs/adr/`.

# Этап 2: веб-панель

## Поток данных панели

`загрузка → Storage (queued) → Worker → pipeline.transcript_for → pipeline.analyze_raw → Storage.save_result (done) → stats → страницы`

- `web/routes/upload.py`: `POST /api/upload` кладёт файл в `audio/` и строку в `calls` (дубликат по sha256 — существующая запись), будит воркер.
- `worker.Worker` берёт самый старый `queued` (`Storage.next_queued`, атомарно в `processing`), расшифровывает с кэшем `<data_dir>/cache`, оценивает, сохраняет. Ошибка → `error`, доступно «Повторить».
- В БД лежит сырая расшифровка (`RawTranscript`) и авто-оценка; роли и оценка пересчитываются из сырой без модели: `rescoring.rescore_call/rescore_all/swap_sides/reapply_template`.
- Ручная правка — отдельная таблица `corrections`; авто-оценка не перезаписывается, действующий `CallRecord.score` получается через `scoring.summarize` по авто-оценке с правками.
- `stats` — чистые функции над `CallRecord`; `web/routes/*` готовят данные из `stats`, `web/charts.py` рисует SVG, шаблоны только выводят.
- Выгрузка `GET /calls/export.xlsx` — те же фильтры, через `stats.to_call_results` → `report_xlsx.write_xlsx`.

## Границы модулей

- `storage.py` владеет схемой SQLite и файлами папки данных; SQL нигде больше не пишется. Время в UTC, наружу — `datetime`.
- `worker.py` не знает про веб; веб видит его через `Worker` (`wake`, `request_rescore`, `rescore_status`) и допускает `worker=None` (тесты, `import`).
- `rescoring.py` ходит только в `Storage` и `pipeline`, моделью не пользуется.
- `stats.py` не знает про SQLite и FastAPI; `Storage.list_calls` с фильтром делегирует в `stats.apply_filters`.
- `web/` не пишет SQL и не считает баллы: только `Storage`, `stats`, `rescoring`, `Worker`.
- `pipeline.py` — общий конвейер для `cli.analyze` и воркера, без typer/rich.
- Рабочие yaml читает `web.deps.WorkingSettings` (перечитывает при смене mtime+size); пишут `checklist.save_checklist`/`config.save_config` атомарно с проверкой.

## Соглашения

- Вход — middleware `auth.require_login` на всё приложение; публичны только `/login` и `/static/*`. Без сессии: `/api/*`, `/audio/*`, `*.xlsx` → 401 JSON; остальное, включая POST форм, → 303 на `/login?next=…`.
- Действующая оценка — `CallRecord.score` (с правками); списки, статистика и выгрузка берут её, не `auto_items`. Правки считаются по весам из `basis_json`, сохранённым вместе с авто-оценкой, до переоценки.
- Одна очередь в БД и один поток-воркер (`_job_lock` задачи, `_state_lock` флага переоценки); `requeue_interrupted` при старте возвращает `processing` в `queued`.
- Ошибки: HTML-страницы — страница в каркасе `base.html`, `/api` и `/audio` — JSON; `CallScopeError` показывает `message_ru`.
- Период выбора хранится в сессии (`web.deps.get_period_choice`, по умолчанию 4 недели); дата звонка — `stats.call_date`.
- Страницы рисует `deps.render(request, name, ctx, active=...)`; шаблоны без внешних URL.
- Тесты веба — `TestClient(create_app(settings, worker=None|фейк))`, вход через `/login`; `Storage` на `tmp_path`, `Worker.run_once` синхронно.
- Решения с причинами — `docs/adr/0007`–`0012`.

