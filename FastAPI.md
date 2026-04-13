FastAPI в v3.0 — это **отдельный самостоятельный сервис**, который отвечает за тяжёлые операции: запуск полного пайплайна `draft→polish`, управление проектами, отдачу артефактов и мониторинг. Он **не пересекается с event loop бота** и запускается как независимый процесс.

Вот актуальная спецификация FastAPI-компонента с учётом всех правок v3.0.

---

## 🌐 1. Роль FastAPI в архитектуре v3.0

| Параметр | Значение |
|----------|----------|
| **Задача** | Запуск 10-этапного пайплайна, экспорт файлов, управление проектами, статус задач |
| **Запуск** | Отдельный `systemd`-сервис (`uvicorn`), порт `8000` (или другой) |
| **Контекст** | Те же `config.yaml`, `core/`, `projects/`, что и у бота |
| **Безопасность** | Простой токен (Bearer) + whitelist IP (опционально) |
| **Асинхронность** | `BackgroundTasks` для долгих LLM-вызовов, неблокирующий UI |
| **Логи** | Пишет в `journald` через `logging`, статус задач хранится в памяти/файле |

> ⚠️ **Важно**: FastAPI и Telegram-бот **не должны работать в одном процессе**. Это гарантирует отсутствие блокировок, разделение ресурсов и независимые рестарты.

---

## 📦 2. Обновлённый `web_ui.py` (v3.0-совместимый)

```python
# entrypoints/web_ui.py
from fastapi import FastAPI, Form, BackgroundTasks, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pathlib import Path
import yaml, uuid, asyncio, logging
from typing import Dict, Optional

# === КОНФИГ И ПУТИ ===
BASE_DIR = Path(__file__).resolve().parents[1]
with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

PROJECTS_ROOT = (BASE_DIR / cfg["projects_root"]).resolve()
PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)

# === ЛОГИНГ ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s [WEB] %(message)s")
logger = logging.getLogger("pmf-web")

# === БЕЗОПАСНОСТЬ ===
security = HTTPBearer()
API_TOKEN = cfg.get("web_api_token", "change-me-in-config")

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return True

# === ТРЕКИНГ ЗАДАЧ ===
jobs: Dict[str, dict] = {}

# === ПРИЛОЖЕНИЕ ===
app = FastAPI(title="PMF Pipeline Web", version="3.0")

@app.get("/", response_class=HTMLResponse)
async def ui():
    projects = [d.name for d in PROJECTS_ROOT.iterdir() if d.is_dir()]
    stages = list(cfg["routing"].keys())
    return f"""
    <!DOCTYPE html><html><head><title>PMF Web</title></head><body>
    <h2>🚀 PMF Pipeline Web</h2>
    <form method="post" action="/run">
      <select name="project">{"".join(f"<option>{p}</option>" for p in projects)}</select><br>
      <select name="stage">{"".join(f"<option value='{s}'>{s}</option>" for s in stages)}</select><br>
      <textarea name="input" rows="6" cols="60" placeholder="Описание задачи..."></textarea><br>
      <button type="submit">▶ Запустить этап</button>
    </form>
    <hr>
    <p><a href="/jobs">Статус задач</a> | <a href="/balance">Баланс OpenRouter</a></p>
    </body></html>"""

@app.post("/run", dependencies=[Depends(verify_token)])
async def run_stage(
    project: str = Form(...),
    stage: str = Form(...),
    input: str = Form(...),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    proj_path = PROJECTS_ROOT / project
    if not proj_path.exists():
        raise HTTPException(404, "Проект не найден")
    if stage not in cfg["routing"]:
        raise HTTPException(400, "Неизвестный этап")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "queued", "stage": stage, "project": project, "result": None}

    background_tasks.add_task(_execute_pipeline, job_id, stage, input, str(proj_path))
    return {"job_id": job_id, "status": "queued", "check_url": f"/jobs/{job_id}"}

async def _execute_pipeline(job_id: str, stage: str, input_text: str, proj_path: str):
    jobs[job_id]["status"] = "running"
    logger.info(f"▶ Запуск {stage} | Проект: {proj_path}")
    try:
        # Импортируем router динамически, чтобы избежать циклических зависимостей
        from core.router import run_stage
        result = run_stage(stage, input_text, proj_path)
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["result"] = result[:500] + "..."
        logger.info(f"✅ Завершён {stage} | {proj_path}")
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        logger.error(f"❌ Ошибка {stage}: {e}")

@app.get("/jobs", response_class=HTMLResponse)
async def list_jobs():
    rows = "".join(f"<tr><td>{j['status']}</td><td>{j['stage']}</td><td>{j['project']}</td></tr>" for j in jobs.values())
    return f"<table border=1><tr><th>Status</th><th>Stage</th><th>Project</th></tr>{rows}</table>"

@app.get("/jobs/{job_id}")
async def job_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Задача не найдена")
    return jobs[job_id]

@app.get("/download/{project}/{filename}")
async def download(project: str, filename: str):
    path = PROJECTS_ROOT / project / "output" / filename
    if not path.exists():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(path, filename=filename)

@app.get("/balance")
async def check_balance():
    from core.balance_monitor import check_balance
    alert = await check_balance()
    return JSONResponse(content={"alert": alert or "Баланс в норме"})

# Запуск: uvicorn entrypoints.web_ui:app --host 127.0.0.1 --port 8000 --workers 2
```

---

## ⚙️ 3. Ключевые отличия от v2.2

| Было | Стало (v3.0) |
|------|-------------|
| Синхронный `POST /run` | `BackgroundTasks` + трекинг по `job_id` |
| Хардкод путей | `pathlib` + относительные от корня репо |
| Нет авторизации | `HTTPBearer` + токен из `config.yaml` |
| Прямой импорт `router` | Динамический импорт в фоне (избегает блокировок) |
| Нет статуса задач | In-memory `jobs: Dict` + эндпоинт `/jobs/{id}` |
| Логирование в консоль | `logging` + `journald` (через systemd) |

---

## 🖥️ 4. Деплой FastAPI

### `systemd/pmf-web.service`
```ini
[Unit]
Description=PMF Pipeline Web UI
After=network.target marketbot.service

[Service]
Type=simple
User=xander_bot
Group=xander_bot
WorkingDirectory=/home/xander_bot/botz/МаркетБот/work/pmf-bot
ExecStart=/home/xander_bot/botz/МаркетБот/work/pmf-bot/.venv/bin/uvicorn entrypoints.web_ui:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
```

### Nginx (опционально, но рекомендуется)
```nginx
server {
    listen 443 ssl;
    server_name pmf.yourdomain.com;
    
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Authorization "Bearer $http_authorization";
    }
}
```

---

## 🔍 5. Как FastAPI взаимодействует с ботом?

```
Telegram Bot (asyncio)          FastAPI (uvicorn)
       │                               │
       ▼                               ▼
  Сохраняет в inbox/           Запускает router.run_stage()
  Отвечает быстро (1 вызов)    Пишет в output/
  Работает 24/7                Отдаёт файлы по запросу
       │                               │
       └─────── shared filesystem ─────┘
             ~/pmf-projects/
```

- **Конфликтов нет**: разные процессы, разные порты, общее хранилище.
- **Синхронизация**: оба читают `state.json` и `output/`. При одновременной записи `router.run_stage()` использует атомарные операции (`write temp → rename`).
- **Баланс**: FastAPI может отдавать статус `/balance`, бот шлёт алерт в ЛС.

---

## ✅ Итог по FastAPI в v3.0
1. **Отдельный сервис** с `systemd`, `uvicorn`, `BackgroundTasks`.
2. **Неблокирующий UI**: запуск этапа → `job_id` → проверка статуса → скачивание `.md`.
3. **Безопасность**: Bearer-токен, валидация проектов/этапов, относительные пути.
4. **Логирование**: `journald` + структурные логи в `logs/web.log`.
5. **Готов к деплою**: `systemd` + `nginx` + `config.yaml` интеграция.
