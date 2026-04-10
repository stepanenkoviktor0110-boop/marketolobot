# Tech Stack

## Язык
- Python 3.10+

## Зависимости
- aiogram — Telegram Bot API
- FastAPI + uvicorn — Web UI
- pyyaml — конфигурация
- requests — HTTP к LLM API
- python-multipart — загрузка файлов в FastAPI

## LLM провайдеры
- **OpenRouter** (draft): Qwen 2.5 72B (`qwen/qwen-2.5-72b-instruct`) — доступен из РФ
- **OpenRouter** (polish): DeepSeek V3 (`deepseek/deepseek-chat-v3-0324`) — доступен из РФ

## Инфраструктура
- Деплой: systemd/docker + nginx
- Бэкап: cron tar
- Хранение: файловая система (без БД)
