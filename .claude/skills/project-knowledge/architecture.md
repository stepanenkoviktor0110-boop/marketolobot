# Архитектура

## Структура репозитория
```
pmf-pipeline/
├── config.yaml              # Ключи, модели, роутинг, настройки групп
├── core/
│   ├── storage.py           # state.json, артефакты, кэш
│   ├── router.py            # draft → polish, выбор моделей
│   ├── processor.py         # Контекст, промпты, чанкинг/экспорт
│   └── group_utils.py       # Привязка групп, триггеры, батчинг
├── entrypoints/
│   ├── telegram_bot.py      # Личный бот
│   ├── web_ui.py            # FastAPI
│   └── group_listener.py    # Хендлеры супергрупп
├── requirements.txt
└── projects/                # ~/pmf-projects/
    └── {project_name}/
        ├── state.json
        ├── hypothesis.md
        ├── research/
        ├── output/
        └── group_cache/
```

## Потоки данных
1. Вход (Telegram/Web/Group) → Core Processor → Router (draft → polish) → LLM APIs
2. Draft: дешёвая модель (DeepSeek/Qwen через OpenRouter) → JSON-скелет
3. Polish: Claude (Sonnet/Opus) → финальный Markdown-отчёт
4. Результат сохраняется в `output/` проекта

## Роутинг по этапам
10 этапов PMF, каждый имеет свою пару моделей draft/polish в config.yaml.
Этапы: hypothesis, research, risks, dvf, interview, execute, insights, narrative, metrics, decision.

## Безопасность
- owner_id проверка на всех входах
- Группы линкуются только allowed_admins
- Изоляция проектов по папкам
- Счётчик токенов в state.json
