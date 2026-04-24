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
│   ├── balance_monitor.py   # Стаб (subscription mode, no-op для совместимости импортов)
│   ├── context_builder.py   # Сборка контекста для LLM (summary + group_context tail)
│   ├── rag_engine.py        # ChromaDB RAG — индексация и поиск по проекту
│   ├── prompts.py           # Промпты по этапам
│   └── logging_config.py    # Настройка логгера
├── entrypoints/
│   ├── telegram_bot.py      # Telegram-бот (aiogram 3.x)
│   └── web_ui.py            # FastAPI v4.0
├── data/
│   ├── activity.json        # Лог действий Web UI (100 последних, flock at R-M-W)
│   ├── tasks.json           # Пайплайн-задачи Web UI (persistent, flock on mutate)
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
| GET | / | cookie | Dashboard (3 таба: Работа / Результаты / Команда); без валидного `pmf_auth` cookie → 303 на `/login` |
| GET | /login | — | Форма ввода токена |
| POST | /login | — | Проверяет токен, ставит HttpOnly `pmf_auth` cookie (30 дней), 303 на `/` |
| POST | /logout | — | Стирает `pmf_auth` cookie, 303 на `/login` |
| POST | /api/run | owner | Запуск этапа PMF |
| POST | /api/jobs/{id}/retry | owner | Повтор этапа с теми же параметрами (если `input` сохранён) |
| DELETE | /api/jobs/{id} | owner | Удалить запись задачи (файл результата остаётся) |
| POST | /api/ctx | shared | Очистка контекста |
| POST | /api/ctx_and_index | owner | Обработка контекста + RAG индексация |
| POST | /api/index | owner | Только RAG-индексация |
| GET | /api/index/{project} | shared | Статистика индекса |
| GET | /api/jobs | shared | Список задач |
| POST | /api/chat | shared | AI-чат с RAG контекстом |
| GET | /api/pmf_score | shared | PMF Score проекта |
| GET | /api/activity | shared | Лента активности |
| POST | /api/voice | shared | Голосовая заметка + транскрибация |
| GET | /api/tags | shared | Теги проекта |
| POST | /api/schedule | owner | Настройка расписания |
| GET | /api/balance | shared | Маркер subscription mode (`{ok, mode, provider}`) |
| GET | /api/guests | owner | Гости, активность, токены, участники групп |
| POST | /api/invite | owner | Создать invite-токен |
| POST | /api/revoke | owner | Отозвать доступ гостя |
| POST | /api/feedback | shared | Сохранить замечание |
| GET | /api/archive/{p} | shared | Все `.md` проекта, сгруппированные по категориям (whitelist: output / hypotheses / brainstorm / ratings / docs / inbox + корень) |
| DELETE | /api/archive/{p}/{path} | owner | Удалить `.md` из архива (только `.md`, path traversal заблокирован) |
| GET | /view/{p}/{path} | shared | Рендер `.md` в HTML через marked+DOMPurify (типовой target кнопки «Открыть») |
| GET | /download/{p}/{path} | shared | Скачать файл |

## Auth
- Токены из config.yaml секции `webui:`:
  - `owner_token` — полный доступ (CRUD, запуск этапов, управление гостями)
  - `shared_token` — просмотр, чат, контекст, голос
- Два механизма на Web UI:
  - **Cookie-session (браузер):** POST `/login` → HttpOnly `pmf_auth` cookie (30 дней, `SameSite=Strict`, `Secure` при https/x-forwarded-proto=https). Дашборд редиректит на `/login` без cookie. JS-обёртка `fetch` авторедиректит на `/login` при 401.
  - **Bearer (скрипты/curl):** `Authorization: Bearer <token>` — совместимость сохранена для всех `/api/*`.
- Токен больше не встраивается в HTML (сорс страницы чистый; в cookie — `HttpOnly`, JS прочитать не может).
- Telegram: `is_allowed()` — owner_id + зарегистрированные гости

## Watchdog
- Запуск: `_check_single_instance()` читает `data/bot.pid` + `data/bot.heartbeat`
- Heartbeat свежий (<30 мин) → убить себя (мы новее), heartbeat протухший → SIGTERM старому
- `_heartbeat_loop()` обновляет heartbeat каждые 30 минут

## Безопасность
- owner_id проверка на всех входах Telegram
- Path traversal защита через общий `_resolve_project_file()` (`/download`, `/view`, `DELETE /api/archive`): reject пустого/`.`/`..`/slash в project + `.resolve()` + prefix check
- `/view` санитизирует Markdown через DOMPurify + SRI-pinned marked@12.0.2 и dompurify@3.0.9; кнопка «Открыть» использует `window.open(..., 'noopener,noreferrer')`
- Токен из HTML убран. Auth — HttpOnly cookie (см. секцию Auth)
- Изоляция проектов по папкам

## Конкурентность
- `data/tasks.json` и `data/activity.json`: все R-M-W под `fcntl.LOCK_EX`, чтобы два uvicorn-воркера не перезаписывали друг друга
- Мутации задач идут через `core/task_storage.atomic_update_task()` (load → mutate → save под одним lock'ом); `_tasks_cache` — optimistic snapshot для `GET /api/jobs`
- `data/bot_feedback.md` дописывается под lock
