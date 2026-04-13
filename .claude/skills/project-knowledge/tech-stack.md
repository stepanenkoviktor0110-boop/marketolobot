# Tech Stack

## Язык и рантайм
- Python 3.12 (venv: `.venv/`)

## Зависимости
- aiogram 3.x — Telegram Bot API (frozen Pydantic models — нельзя мутировать поля Message)
- FastAPI + uvicorn — Web UI (workers=2, host=127.0.0.1:8080)
- httpx — async HTTP для LLM API (заменяет requests в router.py)
- pyyaml — конфигурация
- faster-whisper 1.2.1 — локальная транскрибация (model=small, device=cpu, compute_type=int8)
- chromadb — векторная БД для RAG (`chroma_db/` в корне проекта)
- python-multipart — загрузка файлов в FastAPI

## LLM провайдеры
- **OpenRouter** — все LLM-вызовы через единый endpoint
  - Draft (быстрый): `qwen/qwen-2.5-72b-instruct`
  - Polish (качество): `deepseek/deepseek-chat-v3-0324`
  - Telegram lite: draft model для chat/hypothesize/brainstorm/rate
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
- `llm.openrouter` — api_key, base_url, draft_model, polish_model, timeout
- `webui.owner_token` / `webui.shared_token` — токены доступа к Web UI
- `routing` — модели draft/polish per PMF-этап
