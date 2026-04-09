# 🚀 PMF Pipeline: Техническая спецификация v2.0

> **🔗 Источник и ядро методологии**  
> Вся логика этапов, фреймворки (`7 Dimensions`, `DVF`, `Sean Ellis`, `Levels of PMF`, `7 Powers`) и архитектура состояния основаны на открытом скилле **[PMF Skill by Alena Zaharova UX](https://github.com/alenazaharovaux/share/tree/main/skills/pmf)**.  
> Данный документ описывает **личную многоканальную адаптацию** с гибридным роутингом `draft → polish` и единым файловым хранилищем.

---

## 🧩 1. Архитектура системы

```
[Telegram Bot (личные заметки)] ──┐
                                  ├──► [Core Processor] ──► [Router (draft→polish)] ──► [LLM APIs]
[Web UI (структурированный ввод)] ─┘                                                        │
[Group Listener (пассивный сбор)] ─► [Queue & Batching] ────────────────────────────────────┘
                                   │
                                   ▼
                    [~/pmf-projects/{project_name}/]
                    (state.json, артефакты этапов, output/, group_cache/)
```

### 🔑 Ключевые принципы
- **Единый источник истины**: прогресс и данные хранятся **только в файловой системе**. Нет БД, нет внешних сервисов.
- **Три входа, одно ядро**: все каналы пишут/читают из одной папки проекта.
- **Жёсткий линейный пайплайн**: все 10 этапов проходят через `draft → polish`. Никаких `skip`.
- **Личное использование**: проверка `owner_id`, нет коммерческого биллинга, изоляция проектов.

---

## 📂 2. Структура репозитория

```
pmf-pipeline/
├── config.yaml                  # Ключи, модели, роутинг, настройки групп
├── core/
│   ├── storage.py               # Работа с state.json, артефактами, кэшем
│   ├── router.py                # Логика draft → polish, выбор моделей
│   ├── processor.py             # Сборка контекста, промпты, чанкинг/экспорт
│   └── group_utils.py           # Привязка групп, триггеры, батчинг очереди
├── entrypoints/
│   ├── telegram_bot.py          # Личный бот (/new, /use, заметки)
│   ├── web_ui.py                # FastAPI оболочка
│   └── group_listener.py        # Хендлеры для супергрупп
├── requirements.txt             # aiogram, fastapi, uvicorn, pyyaml, requests, python-multipart
└── projects/                    # ~/pmf-projects/ (настраиваемый путь)
    └── {project_name}/
        ├── state.json
        ├── hypothesis.md
        ├── research/
        ├── output/              # Сгенерированные выжимки
        └── group_cache/
            ├── queue.json
            └── processed.log
```

---

## ⚙️ 3. Конфигурация (`config.yaml`)

```yaml
owner_id: 123456789
projects_root: ~/pmf-projects

llm:
  openrouter:
    base_url: https://openrouter.ai/api/v1
    api_key: sk-or-v1-...
    temperature: 0.5
  claude:
    base_url: https://api.anthropic.com/v1
    api_key: sk-ant-...
    temperature: 0.3
    max_tokens: 4000

# Полная маршрутизация: 10 этапов, все через draft → polish
routing:
  1_hypothesis:  {draft: deepseek/deepseek-v3.2, polish: claude-sonnet-4}
  2_research:    {draft: qwen/qwen3.5-plus, polish: claude-opus-4.6}
  3_risks:       {draft: deepseek/deepseek-v3.2, polish: claude-sonnet-4}
  4_dvf:         {draft: deepseek/deepseek-v3.2, polish: claude-opus-4.6}
  5_interview:   {draft: deepseek/deepseek-v3.2, polish: claude-sonnet-4}
  6_execute:     {draft: qwen/qwen3.5-plus, polish: claude-sonnet-4}
  7_insights:    {draft: deepseek/deepseek-v3.2, polish: claude-sonnet-4}
  8_narrative:   {draft: deepseek/deepseek-v3.2, polish: claude-opus-4.6}
  9_metrics:     {draft: qwen/qwen3.5-plus, polish: claude-sonnet-4}
  10_decision:   {draft: deepseek/deepseek-v3.2, polish: claude-opus-4.6}

group:
  trigger_mode: mention  # mention | keywords | batch
  keywords: ["/insight", "#pmf", "важно:", "фидбек:", "проблема:"]
  output_channel: dm     # dm | group | both
  batch_size: 15
  batch_age_hours: 2
  anonymize_users: true
  ignore_media: true
  allowed_admins: [123456789, 987654321]
```

---

## 🧠 4. Ядро логики (`core/`)

### 4.1. Хранилище (`storage.py`)
```python
import os, json
from datetime import datetime

def load_state(project_path: str) -> dict:
    state_file = os.path.join(project_path, "state.json")
    if os.path.exists(state_file):
        with open(state_file) as f: return json.load(f)
    return {"current_stage": "1_hypothesis", "last_active": None, "tokens_used": 0}

def save_state(project_path: str, state: dict):
    state["last_active"] = datetime.now().isoformat()
    with open(os.path.join(project_path, "state.json"), "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def get_context(project_path: str, stage: str) -> str:
    files = []
    for f in os.listdir(project_path):
        if f.endswith((".md", ".json")) and f != "state.json":
            with open(os.path.join(project_path, f)) as fh:
                files.append(f"## {f}\n{fh.read()[:1500]}")
    return "\n\n".join(files)
```

### 4.2. Маршрутизатор (`router.py`)
```python
import requests, json, yaml

with open("config.yaml") as f:
    cfg = yaml.safe_load(f)

def run_stage(stage: str, user_input: str, project_path: str) -> str:
    route = cfg["routing"][stage]
    draft_json = _call_draft(stage, user_input, project_path, route["draft"])
    final_md  = _call_polish(stage, draft_json, project_path, route["polish"])
    _save_artifact(project_path, f"{stage}_draft.json", json.dumps(draft_json, indent=2, ensure_ascii=False))
    _save_artifact(project_path, f"{stage}_final.md", final_md)
    return final_md

def _call_draft(stage, inp, proj, model):
    prompt = f"Этап {stage}. Контекст: {_load_proj_files(proj)}. Задача: {inp}. Верни СТРОГО JSON: {{summary, items[], next_step, metrics[]}}"
    return _api_call(model, prompt, "openrouter", fmt="json")

def _call_polish(stage, draft, proj, model):
    prompt = f"Отредактируй черновик этапа {stage}. Не меняй факты. Усиль аргументацию, добавь стратегический контекст. Сохрани JSON-структуру. Верни Markdown-отчёт."
    return _api_call(model, prompt + f"\nЧерновик:\n{json.dumps(draft, ensure_ascii=False)}", "claude", fmt="text")

def _api_call(model, prompt, provider, fmt="text"):
    prov = cfg["llm"][provider]
    headers = {"Authorization": f"Bearer {prov['api_key']}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": prov.get("max_tokens", 4000), "temperature": prov["temperature"]}
    r = requests.post(f"{prov['base_url']}/chat/completions", headers=headers, json=payload)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    return json.loads(text) if fmt == "json" else text
```

### 4.3. Процессор и вывод (`processor.py`)
```python
def chunk_text(text: str, max_len: int = 3000) -> list[str]:
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            chunks.append(current.strip())
            current = line
        else: current += "\n" + line
    if current.strip(): chunks.append(current.strip())
    return chunks

def _save_artifact(proj: str, filename: str, content: str):
    os.makedirs(os.path.join(proj, "output"), exist_ok=True)
    with open(os.path.join(proj, "output", filename), "w") as f: f.write(content)
```

### 4.4. Групповой модуль (`group_utils.py`)
```python
def link_group(group_id: int, project: str, admin_id: int) -> bool:
    if admin_id not in cfg["group"]["allowed_admins"]: return False
    groups = _load_groups()
    groups[str(group_id)] = project
    _save_groups(groups)
    return True

def should_process(msg, mode):
    if msg.from_user.is_bot: return False
    if mode == "mention": return any(e.user.id == bot.id for e in msg.entities or [] if e.type == "mention")
    if mode == "keywords": return any(k in (msg.text or "").lower() for k in cfg["group"]["keywords"])
    return True

def process_batch(project: str, queue: list):
    prompt = f"Анализируй групповое обсуждение проекта {project}. Выдели 3-5 инсайтов, свяжи с текущим этапом, предложи действия. JSON: {{insights[], actions[]}}"
    return router._call_draft("group_insights", "\n".join([f"- {m['text']}" for m in queue[-cfg['group']['batch_size']:]]), project, cfg['routing']['7_insights']['draft'])
```

---

## 🚪 5. Точки входа (`entrypoints/`)

### 5.1. Telegram-бот (личный)
- **Команды**: `/new <name>`, `/use <name>`, `/continue`, `/export`
- **Защита**: `if msg.from_user.id != cfg["owner_id"]: return`
- **Логика**: текст → `router.run_stage()` → `chunk_text()` → отправка частями или `.md` файлом.
- **Файлы/голос**: сохраняются в `projects/{name}/attachments/`, путь добавляется в контекст.

### 5.2. Web UI (`web_ui.py`)
```python
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, FileResponse
import core.router as router, core.storage as storage

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
def ui():
    projs = [d for d in os.listdir(cfg["projects_root"]) if os.path.isdir(os.path.join(cfg["projects_root"], d))]
    return f"""<form method="post" action="/run">
      <select name="project">{"".join(f"<option>{p}</option>" for p in projs)}</select><br>
      <textarea name="input" rows="8" cols="60"></textarea><br>
      <button type="submit">Обработать</button></form>"""

@app.post("/run")
def run(project: str = Form(...), input: str = Form(...)):
    state = storage.load_state(os.path.join(cfg["projects_root"], project))
    res = router.run_stage(state["current_stage"], input, os.path.join(cfg["projects_root"], project))
    path = os.path.join(cfg["projects_root"], project, "output", f"{state['current_stage']}_final.md")
    return f"<pre>{res[:2000]}</pre><br><a href='/dl/{project}'>Скачать .md</a>"

@app.get("/dl/{project}")
def dl(project: str): return FileResponse(os.path.join(cfg["projects_root"], project, "output"))
```

### 5.3. Group Listener (`group_listener.py`)
- Регистрируется на `F.chat.type.in_(["group", "supergroup"])`
- Проверяет привязку: `project = groups.get(str(msg.chat.id))`
- Фильтрует по триггеру (`@mention`/`keywords`/`batch`)
- Отправляет в `processor.py` → ответ в ЛС (`dm`) или чат (`group`)
- Поддерживает `/link_project`, `/group_process`, `/group_clear`

---

## 🛠 6. План реализации

| Шаг | Задача | Статус |
|-----|--------|--------|
| 1 | Инициализация репозитория, `config.yaml`, структура папок | ⬜ |
| 2 | `core/storage.py` + `core/processor.py` | ⬜ |
| 3 | `core/router.py` (адаптация под OpenRouter + Anthropic) | ⬜ |
| 4 | `entrypoints/telegram_bot.py` (личные команды, защита) | ⬜ |
| 5 | `entrypoints/web_ui.py` (FastAPI + HTML) | ⬜ |
| 6 | `core/group_utils.py` + `entrypoints/group_listener.py` | ⬜ |
| 7 | Интеграционное тестирование (заметка → draft → polish → файл) | ⬜ |
| 8 | Деплой (`systemd`/`docker` + `nginx` + `cron` бэкап) | ⬜ |

**Запуск**:
```bash
pip install aiogram fastapi uvicorn pyyaml requests python-multipart
uvicorn entrypoints.web_ui:app --host 0.0.0.0 --port 8000 &
python entrypoints/telegram_bot.py
```

---

## 🔒 7. Безопасность и ограничения

- **Доступ**: жёсткий `owner_id`, группы линкуются только `allowed_admins`.
- **Изоляция**: кросс-контекст запрещён. Каждый проект в отдельной папке.
- **Токены**: счётчик в `state.json`. При превышении бюджета → лог + уведомление.
- **Лимит Telegram**: 4096 символов → обходится `chunk_text()` или отправкой `.md`.
- **ToS**: OpenRouter API + Claude API для личных целей соответствуют условиям. Сбор из рабочих чатов допустим при информировании участников.
- **Бэкап**: `cron 0 2 * * * tar -czf /backup/pmf_$(date +\%F).tar.gz ~/pmf-projects/`

---

## 📎 8. Примечания для разработчика

1. **JSON-скелет обязателен**: в промпте к `draft` всегда требуй строгую структуру. Без этого `polish` получит невалидный вход.
2. **Адаптер провайдеров**: OpenRouter использует формат OpenAI, Anthropic — свой. Рекомендуется обёртка `litellm` или явная подготовка payload под каждый провайдер в `_api_call()`.
3. **Контекст не дублируй**: передавай в LLM только `state.json`, файлы текущего этапа и 2-3 последних артефакта. Обрезай по 1500 символов на файл.
4. **Фоновая обработка**: тяжёлые этапы запускай через `asyncio.create_task()`, возвращай «⏳ В работе, отправлю файл по готовности».
5. **Отладка**: всегда сохраняй `_draft.json` перед `polish`. При рассинхроне логики можно сделать `diff`.

