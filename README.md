# МаркетБот

Личный инструмент для прохождения 10 этапов PMF-анализа (Product-Market Fit). Три входа — Telegram-личка, Telegram-группа, Web UI — одно ядро обработки. LLM-движок: Claude Code CLI на подписке.

## Структура

| Папка | Что внутри |
|-------|------------|
| `core/` | Pipeline, роутер, RAG, транскрибация, контекст |
| `entrypoints/` | `telegram_bot.py` (aiogram) и `web_ui.py` (FastAPI) |
| `data/` | Состояние ботa: PID, heartbeat, активные сессии, гости, лог активности |
| `projects/` | Проекты пользователя: state, артефакты, контекст групп |
| `chroma_db/` | Векторная БД для RAG |
| `systemd/` | Файлы юнитов systemd (рабочие копии в `/etc/systemd/system/`) |
| `work/` | Бэклог и рабочие заметки |
| `.claude/skills/project-knowledge/` | Подробная документация для AI-агентов |

## Документация

Подробности — в `.claude/skills/project-knowledge/`:
- `overview.md` — что делает бот, входы, режимы
- `architecture.md` — структура репо, потоки данных, эндпоинты, watchdog
- `tech-stack.md` — стек, LLM-движок, зависимости, сервисы
- `deployment.md` — systemd, ротация секретов, логи, откат

## Быстрый старт (на VPS)

1. Склонировать репо на сервер.
2. Создать `config.yaml` из `config.example.yaml`, заполнить токены и секреты.
3. `.venv/bin/pip install -r requirements.txt`
4. Прописать systemd-юниты (см. `systemd/`), `sudo systemctl enable --now pmf-web marketbot`.
5. Web UI: `http://127.0.0.1:8080` с заголовком `Authorization: Bearer <owner_token>`.
