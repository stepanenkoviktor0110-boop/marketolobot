# МаркетБот — PMF Pipeline

## Что это
Личный инструмент для прохождения 10 этапов PMF (Product-Market Fit) анализа с гибридным роутингом draft → polish через несколько LLM.

## Каналы ввода
- **Telegram Bot** — личные заметки, команды `/new`, `/use`, `/continue`, `/export`
- **Web UI** — структурированный ввод через FastAPI
- **Group Listener** — пассивный сбор из Telegram-супергрупп

## Ключевые принципы
- Единый источник истины — файловая система (`~/pmf-projects/{project_name}/`)
- Три входа, одно ядро обработки
- Жёсткий линейный пайплайн: 10 этапов, все через draft → polish
- Личное использование (owner_id), без коммерческого биллинга

## Технологии
- Python (aiogram, FastAPI, uvicorn, pyyaml, requests)
- LLM: OpenRouter (DeepSeek, Qwen) для draft, Claude (Sonnet/Opus) для polish
- Хранение: файловая система (JSON + Markdown), без БД
