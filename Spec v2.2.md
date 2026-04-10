# 🚀 PMF Pipeline: Техническая спецификация v3.0 (Final)

> **🔗 Источник методологии**  
> Ядро этапов, фреймворки (`7 Dimensions`, `DVF`, `Sean Ellis`, `Levels of PMF`, `7 Powers`) и архитектура состояния основаны на **[PMF Skill by Alena Zaharova UX](https://github.com/alenazaharovaux/share/tree/main/skills/pmf)**.  
> **📌 Версия v3.0** включает: инфраструктуру (systemd, venv, относительные пути), голосовую транскрибацию, rolling-контекст групп, контроль баланса, расширенный доступ и логирование в journald.

---

## 🧭 1. Архитектура системы

```
┌─────────────────────────────────────────────────────────────┐
│                        ВХОДЫ                                 │
├──────────────┬──────────────┬────────────────────────────────┤
│ 📱 Telegram  │ 🌐 Web UI    │ 👥 Group Listener              │
│ • Заметки    │ • Полные     │ • Rolling context (300 строк)  │
│ • /hypothesize│ этапы       │ • Голосовые → whisper → текст  │
│ • /brainstorm│ • Экспорт    │ • /link_project                │
│ • Голосовые  │ • Настройка  │                                │
└──────┬───────┴──────┬───────┴───────────────┬────────────────┘
       │              │                        │
       ▼              ▼                        ▼
┌─────────────────────────────────────────────────────────────┐
│                     CORE (единое ядро)                        │
├─────────────────────────────────────────────────────────────┤
│ • storage.py          — state.json, артефакты, rolling ctx  │
│ • router.py           — полный пайплайн draft→polish        │
│ • telegram_ops.py     — лёгкие команды, per-user semaphore  │
│ • group_utils.py      — group_links.json, batch, context    │
│ • voice_transcriber.py— faster-whisper, thread pool, queue  │
│ • balance_monitor.py  — OpenRouter API, <$1 alert           │
│ • logging_config.py   — journald + file rotation            │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                LLM-ПРОВАЙДЕРЫ (v3.0)                         │
├─────────────────────────────────────────────────────────────┤
│ 🟢 Draft:  qwen/qwen-2.5-72b-instruct (OpenRouter)          │
│ 🔵 Polish: deepseek/deepseek-chat (OpenRouter, V3)          │
│ 💰 Billing: OpenRouter credits (баланс <$1 → алерт owner)   │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│           ХРАНИЛИЩЕ (относительные пути)                     │
├─────────────────────────────────────────────────────────────┤
│ • projects/{name}/                                          │
│   ├── state.json                                            │
│   ├── group_context.md  (rolling 300 строк)                 │
│   ├── inbox/          (личные заметки)                      │
│   ├── output/         (артефакты этапов)                    │
│   └── group_links.json (привязка group_id → project)        │
└─────────────────────────────────────────────────────────────┘
```

---

## 📂 2. Структура репозитория

```
pmf-pipeline/
├── config.yaml                  # Ключи, модели, пороги, доступ
├── requirements.txt             # aiogram, fastapi, uvicorn, pyyaml, requests, faster-whisper
├── core/
│   ├── storage.py               # state, rolling context, относительные пути
│   ├── router.py                # draft→polish (10 этапов)
│   ├── telegram_ops.py          # команды, per-user semaphore, reply()
│   ├── group_utils.py           # group_links.json, batch, context sync
│   ├── voice_transcriber.py     # faster-whisper small, ThreadPool, семафор
│   ├── balance_monitor.py       # OpenRouter /credits, alert <$1
│   └── logging_config.py        # journald + file logging
├── entrypoints/
│   ├── telegram_bot.py          # aiogram: команды, доступ, voice, reply
│   └── web_ui.py                # FastAPI: запуск этапов, экспорт
├── systemd/
│   └── marketbot.service        # автозапуск, restart=always, WorkingDirectory
├── .venv/                       # virtualenv (не коммитить)
└── projects/                    # создаётся при старте
    └── {project_name}/
        ├── state.json
        ├── group_context.md
        ├── inbox/
        ├── output/
        └── group_links.json
```

---

## ⚙️ 3. Конфигурация (`config.yaml`)

```yaml
# === ОБЩИЕ ===
owner_id: 123456789
projects_root: projects/  # относительный путь от корня репо
log_file: logs/bot.log
max_context_lines: 300

# === LLM (v3.0: работают из РФ, стабильные) ===
llm:
  openrouter:
    base_url: https://openrouter.ai/api/v1
    api_key: ${OR_KEY}
    draft_model: qwen/qwen-2.5-72b-instruct
    polish_model: deepseek/deepseek-chat
    temperature_draft: 0.6
    temperature_polish: 0.3
    max_tokens: 4000
    balance_threshold_usd: 1.0  # алерт при балансе < $1

# === МАРШРУТИЗАЦИЯ (все этапы draft→polish) ===
routing:
  1_hypothesis:  {draft: draft_model, polish: polish_model}
  2_research:    {draft: draft_model, polish: polish_model}
  3_risks:       {draft: draft_model, polish: polish_model}
  4_dvf:         {draft: draft_model, polish: polish_model}
  5_interview:   {draft: draft_model, polish: polish_model}
  6_execute:     {draft: draft_model, polish: polish_model}
  7_insights:    {draft: draft_model, polish: polish_model}
  8_narrative:   {draft: draft_model, polish: polish_model}
  9_metrics:     {draft: draft_model, polish: polish_model}
  10_decision:   {draft: draft_model, polish: polish_model}

# === TELEGRAM ===
telegram:
  max_context_chars: 2500
  max_response_chars: 2900
  per_user_concurrency: 1  # семафор на пользователя
  commands:
    hypothesize: {model: draft_model}
    brainstorm:  {model: draft_model}
    rate:        {model: draft_model}
    ask:         {model: draft_model}

# === GROUP ===
group:
  trigger_mode: mention
  keywords: ["/insight", "#pmf", "важно:", "фидбек:"]
  output_channel: dm
  batch_size: 15
  anonymize_users: true
  ignore_media: true
  allowed_in_groups: true  # в группах отвечают всем
```

---

## 🧠 4. Ядро логики (`core/`)

### 4.1. Доступ и семафоры (`telegram_ops.py`)
```python
import asyncio
from pathlib import Path

# Per-user semaphore (динамический)
_user_semaphores: dict[int, asyncio.Semaphore] = {}

def get_user_semaphore(user_id: int, limit: int = 1) -> asyncio.Semaphore:
    if user_id not in _user_semaphores:
        _user_semaphores[user_id] = asyncio.Semaphore(limit)
    return _user_semaphores[user_id]

def is_allowed(msg) -> bool:
    """Личка: только owner. Группы: все участники."""
    if msg.chat.type == "private":
        return msg.from_user.id == cfg["owner_id"]
    return cfg["group"]["allowed_in_groups"]
```

### 4.2. Rolling-контекст групп (`storage.py`)
```python
def append_to_group_context(project: str, text: str):
    ctx_path = Path(cfg["projects_root"]) / project / "group_context.md"
    ctx_path.parent.mkdir(parents=True, exist_ok=True)
    
    lines = ctx_path.read_text(encoding="utf-8").splitlines() if ctx_path.exists() else []
    lines.append(text)
    
    # Rolling 300 строк
    if len(lines) > cfg["max_context_lines"]:
        lines = lines[-cfg["max_context_lines"]:]
    
    ctx_path.write_text("\n".join(lines), encoding="utf-8")
```

### 4.3. Голосовая транскрибация (`voice_transcriber.py`)
```python
from faster_whisper import WhisperModel
from concurrent.futures import ThreadPoolExecutor
import asyncio, logging

model = WhisperModel("small", device="cpu", compute_type="int8")
_transcribe_semaphore = asyncio.Semaphore(1)  # один за раз
_executor = ThreadPoolExecutor(max_workers=1)

async def transcribe_voice(file_path: str, user_msg) -> str:
    async with _transcribe_semaphore:
        if user_msg:
            await user_msg.reply("⏳ Транскрибация в очереди...")
        
        loop = asyncio.get_event_loop()
        segments, info = await loop.run_in_executor(_executor, model.transcribe, file_path, beam_size=5)
        return " ".join([s.text for s in segments])
```

### 4.4. Баланс и алерты (`balance_monitor.py`)
```python
import requests

async def check_balance():
    headers = {"Authorization": f"Bearer {cfg['llm']['openrouter']['api_key']}"}
    r = requests.get(f"{cfg['llm']['openrouter']['base_url']}/credits", headers=headers)
    balance = r.json().get("total_credits", 0)
    if balance < cfg["llm"]["openrouter"]["balance_threshold_usd"]:
        # Отправить уведомление owner
        return f"⚠️ Баланс OpenRouter: ${balance:.2f} (порог: ${cfg['llm']['openrouter']['balance_threshold_usd']})"
    return None
```

---

## 🚪 5. Точки входа (`entrypoints/`)

### 5.1. Telegram-бот (`telegram_bot.py`) — ключевые отличия v3.0
```python
# 1. Доступ
if not is_allowed(msg): return

# 2. Reply вместо answer
await msg.reply("✅ Принято")

# 3. Автозапрос проекта
@dp.message(Command("new"))
async def cmd_new(msg: types.Message, command: CommandObject):
    name = command.args.strip().replace(" ", "_") if command.args else None
    if not name:
        await msg.reply("Введи название проекта: /new <имя>")
        return
    # ... создание ...

# 4. /balance
@dp.message(Command("balance"))
async def cmd_balance(msg: types.Message):
    alert = await check_balance()
    await msg.reply(alert or f"💰 Баланс OpenRouter: в норме")

# 5. Голосовые
@dp.message(F.voice)
async def handle_voice(msg: types.Message):
    if not is_allowed(msg): return
    file = await bot.get_file(msg.voice.file_id)
    path = await bot.download_file(file.file_path)
    text = await transcribe_voice(str(path), msg)
    append_to_group_context(project, f"[Голосовое от {msg.from_user.full_name}]: {text}")
    await msg.reply(f"🎙️ Транскрипт: {text[:200]}...")
```

### 5.2. Логирование
- Все критичные действия пишутся в `logs/bot.log` (rotating file)
- `systemd` автоматически направляет stdout/stderr в `journald`
- В чат пишется краткий лог: `💾 Сохранено в контекст | 🔍 Запрос к LLM | 📄 Артефакт создан`

---

## 🌐 6. Web UI (`web_ui.py`)
Без изменений архитектуры. Использует относительные пути:
```python
PROJECTS_ROOT = (Path(__file__).parent.parent / cfg["projects_root"]).resolve()
```

---

## 🛠 7. Инфраструктура и деплой

### 7.1. Virtualenv
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# faster-whisper требует: pip install faster-whisper
```

### 7.2. Systemd (`systemd/marketbot.service`)
```ini
[Unit]
Description=PMF Pipeline Telegram Bot
After=network.target

[Service]
Type=simple
User=xander_bot
Group=xander_bot
WorkingDirectory=/home/xander_bot/botz/МаркетБот/work/pmf-bot
ExecStart=/home/xander_bot/botz/МаркетБот/work/pmf-bot/.venv/bin/python entrypoints/telegram_bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
```

### 7.3. План запуска
| Шаг | Команда / Действие |
|-----|-------------------|
| 1 | `git clone`, `cd pmf-pipeline`, `python3 -m venv .venv && source .venv/bin/activate` |
| 2 | `pip install -r requirements.txt` + `pip install faster-whisper` |
| 3 | Скачать модель `small` (автоматически при первом запуске) |
| 4 | Настроить `config.yaml` (ключи, `owner_id`, пути) |
| 5 | `sudo cp systemd/marketbot.service /etc/systemd/system/` |
| 6 | `sudo systemctl daemon-reload && sudo systemctl enable --now marketbot.service` |
| 7 | `journalctl -u marketbot.service -f` для проверки логов |

---

## 🔒 8. Безопасность и эксплуатация (v3.0)

| Аспект | Мера |
|--------|------|
| **Доступ** | `is_allowed()`: личка → только `owner_id`, группы → все участники |
| **Пути** | Относительные (`pathlib`), никаких хардкодов `/root/...` |
| **Баланс** | Автопроверка OpenRouter `/credits`, алерт в ЛС при `<$1` |
| **Голосовые** | `faster-whisper small`, thread pool, глобальный семафор (1 поток), очередь с уведомлением |
| **Контекст групп** | Rolling 300 строк в `group_context.md`, автосохранение всех сообщений |
| **Concurrency** | Per-user semaphore (`asyncio.Semaphore(1)`), предотвращает race conditions |
| **Логи** | `journald` (stdout/stderr) + rotating file `logs/bot.log` |
| **Reply UX** | `message.reply()` вместо `answer()` → треды, привязка к сообщению |
| **ToS** | OpenRouter API + локальная транскрибация = полное соответствие |

---

## 📎 Примечания для разработчика

1. **Относительные пути**: Везде используй `Path(__file__).resolve().parents[N] / cfg["projects_root"]`. Это спасёт при переносе на другую машину.
2. **`faster-whisper`**: Загружает модель `~/.cache/huggingface`. При первом старте будет задержка ~2-3 мин на скачивание.
3. **Баланс**: OpenRouter отдаёт кредиты в USD. Проверку запускай раз в 4 часа через `asyncio.create_task()`.
4. **Семафоры**: Per-user semaphore гарантирует, что один юзер не забьёт очередь. Глобальный семафор для транскрибации экономит CPU/RAM.
5. **Логи**: `systemd` автоматически ротирует journald. Файловые логи ротируются через `logging.handlers.RotatingFileHandler(maxBytes=5*1024*1024, backupCount=3)`.

---

> ✅ **SPEC v3.0 готов к деплою**.  
> Сохрани как `SPEC.md`, разверни `.venv`, настрой `config.yaml`, загрузи `marketbot.service` в systemd.  
> Все ограничения v2.2 сохранены, добавлены: голос, баланс, rolling-контекст, относительные пути, per-user queue, `reply()`, `journald`.  
> При необходимости сгенерирую полные `.py`-файлы с обработкой ошибок, retry-логикой и структурированным logging.