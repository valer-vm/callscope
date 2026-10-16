# Архитектура CallScope

## Поток данных

`аудио → RawTranscript → Dialogue → CallScore → CallResult → Summary → HTML/XLSX`

| Шаг | Модуль | Вход → выход |
|---|---|---|
| Имя файла | `filenames.parse_filename` | имя → `CallMeta` (не подошло шаблону: менеджер «Не определён») |
| Расшифровка | `asr.Transcriber` (+ `cache.TranscriptCache`) | аудио → `RawTranscript` (сегменты со стороной left/right) |
| Роли | `roles.assign_roles` | `RawTranscript` → `Dialogue` (реплики manager/client) + предупреждения |
| Оценка | `scoring.score_call` | `Dialogue` + `Checklist` → `CallScore` |
| Перепроверка | `semantic.recheck` | только failed-пункты, через Ollama; вызывает CLI, если включено |
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
- Сети нет, кроме скачивания модели whisper и Ollama на localhost при `semantic.enabled`; нелокальный url — предупреждение без запросов.
- Тесты не требуют модели, аудио и сети.
- Оценка без аудио: диалог из `tests/fixtures/*.json` через `Dialogue.from_json`.
- Балл = сумма весов passed / сумма весов (passed+failed) × 100; `na` не считается; потеря звонка — первый failed по порядку чек-листа.
- Роли: приоритет `--manager-channel` → `manager_channel_overrides` → `manager_channel` в конфиге → интро/название компании → первый заговоривший (с предупреждением).
- Моно: стороны угаданы по паузам (`mono_turn_gap`) и «?», диалог помечен `approximate`.
- Кэш расшифровок — `<папка звонков>/.callscope-cache/`; ключ = sha256 файла + модель + версия формата; запись атомарная (tmp + `os.replace`).
- Решения с причинами — `docs/adr/`.
