# Sketch: pmf-bot

## Goal
Личный Telegram-бот — адаптация PMF Skill (github.com/alenazaharovaux) для работы через LLM API вместо Claude Code сессии. Гибридный роутинг draft→polish, всё через OpenRouter.

## What must work
- Telegram-бот с командами `/start`, `/new <name>`, `/use <name>`, `/continue`, `/status`, `/export`
- 10 этапов PMF-пайплайна с адаптированными промптами для LLM API
- Роутер draft→polish: дешёвая модель (DeepSeek/Qwen) генерит JSON-скелет, Claude полирует в Markdown
- Все LLM-вызовы через OpenRouter (единый API, единый токен)
- Детекция текущего этапа по файлам в папке проекта (как в оригинале)
- Диалоговый режим: бот задаёт вопросы этапа, пользователь отвечает текстом
- Сохранение артефактов в файловую систему (state.json, .md файлы по этапам)
- Чанкинг длинных ответов для Telegram (≤4096 символов)
- Защита по owner_id

## Stack
- Python 3.10+
- aiogram 3.x — Telegram Bot API
- httpx — async HTTP к OpenRouter
- pyyaml — конфигурация
- Файловая система для хранения (без БД)

## Notes
- Токена бота пока нет — подставит позже в config.yaml
- OpenRouter токен есть, будет в config.yaml
- owner_id — единственный пользователь
- Промпты этапов адаптируются из оригинального PMF Skill: вместо интерактивной сессии — самодостаточные промпты с контекстом
- Web UI и Group Listener — за пределами скетча
- Базовая методология: github.com/alenazaharovaux/share/tree/main/skills/pmf
