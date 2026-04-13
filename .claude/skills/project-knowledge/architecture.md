# Архитектура

## Структура репозитория
```
МаркетБот/
├── config.yaml              # Ключи, модели, роутинг, webui-настройки
├── core/
│   ├── storage.py           # state.json, артефакты, кэш, get_user_display_name
│   ├── router.py            # draft → polish, telegram_reply, ensure_summary
│   ├── processor.py         # Контекст, промпты, чанкинг/экспорт
│   ├── group_utils.py       # Привязка групп, user registry (users.json)
│   ├── transcriber.py       # faster-whisper, CPU, model=small, язык=ru
│   ├── balance_monitor.py   # Мониторинг баланса OpenRouter (check_balance, start_periodic_check)
│   ├── context_builder.py   # Сборка контекста для LLM (summary + group_context tail)
│   ├── rag_engine.py        # ChromaDB RAG — индексация и поиск по проекту
│   ├── prompts.py           # Промпты по этапам
│   └── logging_config.py    # Настройка логгера
├── entrypoints/
│   ├── telegram_bot.py      # Telegram-бот (aiogram 3.x)
│   └── web_ui.py            # FastAPI v4.0
├── data/
│   ├── activity.json        # Лог действий Web UI (100 последних)
│   ├── bot_feedback.md      # Замечания по боту (4 источника)
│   ├── user_sessions.json   # Активный проект per user_id (персистентно)
│   ├── invite_tokens.json   # Invite-токены для гостей
│   ├── guest_activity.json  # Лог действий гостей
│   ├── bot.pid              # PID текущего экземпляра бота
│   └── bot.heartbeat        # Timestamp последнего heartbeat (watchdog)
├── projects/
│   └── {project_name}/
│       ├── state.json           # Текущий этап, счётчики
│       ├── output/              # {stage}_final.md — результаты этапов
│       ├── docs/
│       │   └── context_digest.md
│       ├── inbox/               # Голосовые заметки: voice_*.webm + voice_*.md
│       ├── group_context.md     # Сырой контекст из группы
│       ├── group_links.json     # {chat_id: project_name} — привязка группы
│       ├── users.json           # {user_id: display_name} — имена участников
│       ├── guests.json          # {user_id: {username, added_at, invited_via}}
│       ├── tags.json            # Теги проекта
│       ├── project_summary.md   # Авто-генерируемое резюме (кешируется)
│       ├── hypotheses/          # {timestamp}.md — результаты /hypothesize
│       ├── brainstorm/          # {timestamp}.md — результаты /brainstorm
│       └── ratings/             # {timestamp}.md — результаты /rate
└── work/
    └── backlog.md
```

## Потоки данных

### PMF pipeline (Web UI / команды)
Telegram/Web → `run_stage()` → `get_context()` → draft LLM (JSON) → polish LLM (Markdown) → `output/{stage}_final.md`

### Telegram lite (личный чат)
Сообщение → `ensure_summary()` → `telegram_reply()` → LLM → ответ  
Для artifact-режимов: только `project_summary.md` в контексте (без истории чата)  
Для chat-режима: `project_summary.md` + последние 50 строк `group_context.md`

### Гостевой доступ
`/share <проект>` → `invite_tokens.json` → пользователь кликает ссылку → `/start inv_XXX` → `guests.json` → персистентный контекст проекта в личке

## Web UI — эндпоинты
| Метод | URL | Доступ | Описание |
|-------|-----|--------|----------|
| GET | / | — | Dashboard |
| POST | /api/run | owner | Запуск этапа PMF |
| POST | /api/ctx | shared | Очистка контекста |
| POST | /api/ctx_and_index | owner | Обработка контекста + RAG индексация |
| GET | /api/jobs | shared | Список задач |
| POST | /api/chat | shared | AI-чат с RAG контекстом |
| GET | /api/pmf_score | shared | PMF Score проекта |
| GET | /api/activity | shared | Лента активности |
| POST | /api/voice | shared | Голосовая заметка + транскрибация |
| GET | /api/tags | shared | Теги проекта |
| POST | /api/schedule | owner | Настройка расписания |
| GET | /api/balance | shared | Баланс OpenRouter |
| GET | /api/guests | owner | Гости, активность, токены, участники групп |
| POST | /api/invite | owner | Создать invite-токен |
| POST | /api/revoke | owner | Отозвать доступ гостя |
| POST | /api/feedback | shared | Сохранить замечание |
| GET | /download/{p}/{path} | shared | Скачать файл |

## Auth
- `HTTPBearer` токены из config.yaml секции `webui:`
- `owner_token` — полный доступ
- `shared_token` — просмотр, чат, контекст
- Telegram: `is_allowed()` — owner_id + зарегистрированные гости

## Watchdog
- Запуск: `_check_single_instance()` читает `data/bot.pid` + `data/bot.heartbeat`
- Heartbeat свежий (<30 мин) → убить себя (мы новее), heartbeat протухший → SIGTERM старому
- `_heartbeat_loop()` обновляет heartbeat каждые 30 минут

## Безопасность
- owner_id проверка на всех входах Telegram
- Path traversal защита в /download: `.resolve()` + prefix check
- Токен инжектируется с сервера в JS, не хранится в localStorage
- Изоляция проектов по папкам
