<!-- autopilot:start -->
# CallScope — локальная речевая аналитика звонков отдела продаж: расшифровка, проверка по чек-листу, отчёты HTML и Excel (этап 1, CLI) и веб-панель с очередью, ручными правками и статистикой (этап 2).

## Команды

В рабочей папке (в пути есть «:») сначала: `export UV_PROJECT_ENVIRONMENT=$HOME/.cache/callscope-venv`; в пути без «:» не нужно.

- `uv sync` — зависимости.
- `uv run ruff check src tests scripts && uv run pytest -q` — линтер и тесты (362 passed, 3 skipped).
- `uv run pytest tests/test_scoring.py -q` — один файл.
- `uv run callscope analyze examples/calls` — отчёт в `reports/<дата_время>/`; ~31 с первый раз, 0 с из кэша.
- `uv run python scripts/generate_examples.py --out <dir>` — 5 m4a за ~34 с (`say`, голос Milena, только macOS).
- `CALLSCOPE_SLOW=1 uv run pytest tests/test_asr.py -q` — медленный тест с реальной моделью.

Веб-панель (этап 2):

- `cp .env.example .env`, вписать `CALLSCOPE_PASSWORD`; затем `uv run callscope serve` — панель на 127.0.0.1:8000 (без пароля — сообщение и выход).
- `uv run callscope demo` — демо-база `callscope-data-demo` (150 звонков, 5 с аудио, ~3 с); `uv run callscope serve --demo` — открыть её; `demo --force` чистит только базу с меткой демо.
- `uv run callscope import <папка>` — загрузить записи из папки в базу панели (`--data-dir` — другая папка данных).
- `uv run pytest tests/web -q` — тесты веба; `CALLSCOPE_SLOW=1 uv run pytest tests/test_e2e_upload.py -q` — сквозной тест на 20 записях с моделью.
- `uv run python scripts/screenshots.py` — пересобрать скриншоты README (`docs/screenshots/`) из демо.
- Docker: `colima start`, `docker-compose up -d --build` (healthy; модель ~1 мин, один раз, том `hf-cache`), `docker-compose down`. Плагин `docker compose` на этой машине не виден — только бинарник `docker-compose`.

## Где что лежит

- Архитектура, поток данных, границы модулей, соглашения — `docs/architecture.md`.
- Решения с причинами — `docs/adr/`; словарь — `CONTEXT.md`.
- Что не доделано и известные баги — `.autopilot/PROGRESS.md`.
- Оркестрация и CLI — `src/callscope/cli.py`; общие типы — `src/callscope/model.py`.
- Правила оценки — `src/callscope/scoring.py`; чек-лист и конфиг — `checklist.yaml`, `callscope.yaml`.
- Тесты оценки без аудио — `tests/fixtures/*.json`; тестовые звонки и их ожидания — `scripts/dialogues.yaml`.
- Кэш расшифровок — `<папка звонков>/.callscope-cache/`.

Веб-панель:

- Архитектура этапа 2 — раздел «Этап 2» в `docs/architecture.md`; решения — `docs/adr/0007`–`0012`.
- Хранилище (SQLite, схема, очередь) — `src/callscope/storage.py`; фон — `worker.py`; общий конвейер — `pipeline.py`.
- Переоценка и смена сторон без модели — `rescoring.py`; статистика и фильтры — `stats.py`.
- Приложение — `src/callscope/web/app.py` (create_app, middleware, обработчики ошибок), вход — `web/auth.py`, зависимости и `render` — `web/deps.py`, настройки из `.env` — `web/settings.py`, SVG-графики — `web/charts.py`.
- Страницы — `web/routes/{upload,overview,managers,calls,settings}.py`, шаблоны — `web/templates/`, статика — `web/static/`.
- Демо-данные — `src/callscope/demo.py`, сырые расшифровки демо — `examples/demo/*.json` (`scripts/make_demo_raw.py`).
- Тесты — `tests/web/` (TestClient), остальное этапа 2 — `tests/test_{storage,worker,rescoring,stats,demo,checklist_save,config_save}.py`.
- Данные панели — папка `CALLSCOPE_DATA_DIR` (по умолчанию `./callscope-data`): `callscope.db`, `audio/`, `cache/`, рабочие `checklist.yaml` и `callscope.yaml`, `.secret_key`.

## Подводные камни

- `checklist.yaml` и `callscope.yaml` в корне и копии в `src/callscope/defaults/` должны совпадать побайтно (проверяют тесты); правь оба.
- faster-whisper 1.2.1 и PyAV 19 несовместимы в `decode_audio`; обход в `src/callscope/asr.py` (`_decode_stereo_av`).
- pymorphy иногда выбирает редкий разбор (омонимы): сравнение идёт по основной лемме, видовые пары и «здравствуйте» — таблицы в `src/callscope/textnorm.py`.
- Whisper пишет «Строй дом» и цифры («20», «10%»): фразы чек-листа и `company_names` должны это терпеть.
- Новую зависимость добавляй только через согласование (в `interfaces.md` — BLOCKED); ffmpeg и системный python3 в системе не рассчитывай.
- Не трогать: `brief*.md`, `.autopilot/`, `CLAUDE.md` (оба брифа и `.autopilot/` только локальные, в git не входят).

Веб-панель:

- Рабочие `checklist.yaml`/`callscope.yaml` панели лежат в папке данных, а не в корне; корневые и `defaults/` — только встроенные значения. Сохранение чек-листа из панели теряет комментарии рабочего yaml.
- Переоценка и смена сторон идут без модели — из сырой расшифровки в БД. Правки считаются по весам, сохранённым с авто-оценкой (`basis_json`), до следующей переоценки.
- Статистика и списки считают по действующей оценке (`CallRecord.score`, с правками), не по `auto_items`.
- Вход — middleware на всё приложение (`auth.require_login`); роутерам защиту добавлять не надо. Без сессии: `/api/*`, `/audio/*`, `*.xlsx` → 401 JSON, остальное (в т. ч. POST форм) → 303 на `/login?next=…`.
- Одна очередь в БД и один поток-воркер; модель создаётся лениво один раз. Не запускай второй воркер на ту же базу.
- Блокировка входа хранится в памяти (сбрасывается при перезапуске). За прокси все клиенты — один адрес: uvicorn запущен с `proxy_headers=False`.
- Ошибки HTML-страниц — страница в каркасе, `/api` и `/audio` — JSON.
- Никаких внешних URL в шаблонах (CDN, шрифты); нет сборки фронтенда — правь `web/static/*` напрямую.
- Нет `CALLSCOPE_PASSWORD` — сервер не стартует; значения секретов не логировать.
- Разработка в этой папке: `callscope-data/` и `callscope-data-demo/` в `.gitignore`; демо не должно стирать рабочую базу.

## Окружение

- `UV_PROJECT_ENVIRONMENT` — вынос venv из пути с «:».
- `CALLSCOPE_SLOW` — `1` включает медленные тесты с моделью.
- `.env` (в `.gitignore`; образец `.env.example`), читается `serve`/`import`:
  - `CALLSCOPE_PASSWORD` — общий пароль панели, обязателен.
  - `CALLSCOPE_SECRET_KEY` — ключ сессий; пусто — генерируется в `<папка данных>/.secret_key`.
  - `CALLSCOPE_DATA_DIR` — папка данных, по умолчанию `./callscope-data`.
  - `CALLSCOPE_HOST` / `CALLSCOPE_PORT` — по умолчанию 127.0.0.1 и 8000.
- Docker (`docker-compose.yml`): данные — том `callscope-data` (`/data`), модель — том `hf-cache` (`HF_HOME`); адрес и порт внутри фиксированы.

## Как здесь работает Autopilot

Сборка ведётся навыком `/autopilot`. Требования, спецификация и таски — в `.autopilot/`.
Прогресс — `.autopilot/dashboard.html`. Требование из `manifest.md` может снять
только пользователь. Прерванную сборку продолжает «продолжи автопилот».

### Правка без автопилота

Мелкий баг или доводка после сборки — обычная сессия, без прогона.

Сначала прочитай:
1. `.autopilot/PROGRESS.md` — что не доделано, известные баги, о чём договорились с пользователем.
2. Что правили после последней сборки — первая строка таблицы в «История git».
3. Только если правка их касается: почему сделано так — `docs/adr/`; что значат слова — `CONTEXT.md`;
   что именно просили — `manifest.md` в папке сборки (указана в начале `PROGRESS.md`).

После правки:
- коммит — по правилам из «История git»;
- строка в «Правки без автопилота» в `PROGRESS.md`: дата — что — почему;
- починенный пункт из «Не доделано» или «Известные баги» — удали;
- договорились с пользователем о том, что следующая сборка не должна пересматривать, —
  строка в «Договорились», его словами.

### История git

Целиком не читай — бери срез под вопрос:

| Вопрос | Команда |
|---|---|
| что правили после последней сборки | `git log --oneline "$(git log -1 --format=%h --grep='^Autopilot: сборка сдана')"..HEAD -- . ':(exclude).autopilot'` |
| всё, что делали ради требования | `git log --oneline --grep="R02"` |
| история файла | `git log --oneline -10 -- <файл>` |
| когда появился или исчез код | `git log --oneline -S "<фрагмент>"` |
| почему строка такая | `git blame -L <с>,<по> <файл>`, затем `git show <sha>` |
| что внутри коммита | `git show --stat <sha>` |

Срез длиннее ~30 коммитов — отдай его субагенту и возьми выжимку в десять строк.

Коммит: заголовок — что (`fix: даты в одном формате`), тело — почему, трейлер
`--trailer "Requirements: R02"`, если правка касается требования из `manifest.md`.
Заголовок `Autopilot: сборка сдана` пишет только автопилот — по нему находят последнюю сборку.
<!-- autopilot:end -->
