# Разработка

```bash
uv run ruff check src tests scripts && uv run pytest -q
```

Тесты не требуют модели, аудио и сети. Медленные тесты (нужна модель whisper в кэше):

- `CALLSCOPE_SLOW=1 uv run pytest tests/test_asr.py -q` — настоящая модель и голос `say`;
- `CALLSCOPE_SLOW=1 uv run pytest tests/test_e2e_upload.py -q -s` — сквозная проверка панели:
  `callscope serve` в фоне, 20 записей через `/api/upload`, время ответа страниц во время
  обработки, дубликат, перезапуск сервера посреди очереди.

Скриншоты для README пересоздаются из демо-режима (нужен Google Chrome):
`uv run python scripts/screenshots.py`.

Как устроен проект: [architecture.md](architecture.md) — поток данных и границы модулей,
[adr/](adr/) — решения с причинами, [CONTEXT.md](CONTEXT.md) — словарь терминов.
Инструкции для ИИ-помощников, с которыми сделан проект, — `AGENTS.md` и `CLAUDE.md` в корне.
