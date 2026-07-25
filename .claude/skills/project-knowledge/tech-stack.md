# Tech Stack

## Язык и рантайм
- Python 3.12 (venv: `.venv/`)

## Зависимости
- aiogram 3.x — Telegram Bot API (frozen Pydantic models — нельзя мутировать поля Message)
- FastAPI + uvicorn — Web UI (workers=2, host=127.0.0.1:8080)
- pyyaml — конфигурация
- faster-whisper 1.2.1 — локальная транскрибация (model=small, device=cpu, compute_type=int8)
- chromadb — векторная БД для RAG (`chroma_db/` в корне проекта)
- python-multipart — загрузка файлов в FastAPI

## LLM движок
- **Claude Code CLI** — все LLM-вызовы через локальный `claude --print --output-format json` subprocess
  - Аутентификация: подписка Pro/Max через OAuth (без API ключа)
  - Бинарь: `~/.npm-global/bin/claude` (override через env `CLAUDE_BIN` или `llm.claude.bin` в config.yaml)
  - Draft (быстрый): `haiku`
  - Polish (качество): `sonnet`
  - Subprocess запускается с `cwd=/tmp` чтобы избежать загрузки project CLAUDE.md
  - Флаги: `--print --no-session-persistence --tools "" --disable-slash-commands --output-format json`
  - Промпт передаётся через stdin (защита от flag confusion + ARG_MAX лимита)
  - Реализация: `core/router.py:_api_call`
- Транскрибация: faster-whisper локально (без API, без токенов)

## Инфраструктура
- VPS: 37.233.82.205, Ubuntu 24.04, user: xander_bot
- RAM: 5.8 GB total (~500 MB под Whisper small при загрузке)
- ffmpeg: `~/.local/bin/ffmpeg` v7.0.2 (нужен для Whisper)
- HTTPS: не настроен, доступ по HTTP напрямую (голосовая запись в браузере не работает без HTTPS)

## Сервисы systemd
- `marketbot.service` — Telegram-бот (`sudo systemctl restart marketbot`)
- `pmf-web.service` — Web UI (`sudo systemctl restart pmf-web`)
- Логи: `sudo journalctl -u marketbot -n 50` / `sudo journalctl -u pmf-web -n 50`

## config.yaml — ключевые секции
- `bot.token` — Telegram Bot token
- `owner_id` — Telegram user ID владельца
- `projects_root` — путь к папке проектов (обычно `projects`)
- `llm.claude` — `bin`, `timeout`, `draft_model`, `polish_model` (для Claude Code CLI)
- `webui.owner_token` / `webui.shared_token` — токены доступа к Web UI (см. deployment.md → Secret Rotation)
- `routing` — модели draft/polish per PMF-этап (значения: `haiku`, `sonnet` или `null` для пропуска этапа)
