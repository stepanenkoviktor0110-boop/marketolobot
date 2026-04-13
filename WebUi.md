Вот полный, production-ready `entrypoints/web_ui.py`. В него встроены:
✅ **Retry-логика** с экспоненциальной задержкой для LLM-вызовов  
✅ **Атомарная запись** файлов (temp → rename)  
✅ **HTML-дашборд** с автообновлением, прогресс-барами и управлением токеном  
✅ **Очистка памяти** (хранит только последние 100 задач)  
✅ **Полная совместимость** с v3.0 (относительные пути, journald, security)

```python
# entrypoints/web_ui.py
import os
import sys
import uuid
import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

import yaml
import requests
from fastapi import FastAPI, Form, BackgroundTasks, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# === 1. CONFIG & PATHS (относительные, v3.0) ===
BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config.yaml"
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

PROJECTS_ROOT = (BASE_DIR / cfg.get("projects_root", "projects")).resolve()
PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)

# === 2. LOGGING (journald-ready) ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WEB] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("pmf-web")

# === 3. SECURITY ===
security = HTTPBearer()
API_TOKEN = cfg.get("web_api_token", os.getenv("PMF_WEB_TOKEN", "change-me-please"))

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid API token")
    return True

# === 4. ATOMIC FILE WRITE ===
def atomic_write(filepath: Path, content: str):
    """Гарантирует целостность: записывает во временный файл → атомарно переименовывает."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=filepath.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(filepath))
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

# === 5. RETRY LOGIC (экспоненциальная задержка) ===
async def run_with_retry(func, args=(), kwargs=None, max_retries=3, base_delay=2.0):
    kwargs = kwargs or {}
    for attempt in range(1, max_retries + 1):
        try:
            if asyncio.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            else:
                return await asyncio.to_thread(func, *args, **kwargs)
        except Exception as e:
            if attempt == max_retries:
                logger.error(f"❌ All {max_retries} retries failed: {e}")
                raise
            wait = base_delay ** (attempt - 1)
            logger.warning(f"⚠️ Attempt {attempt} failed. Retrying in {wait:.1f}s... ({e})")
            await asyncio.sleep(wait)

# === 6. TASK STATE (auto-cleanup) ===
tasks: Dict[str, dict] = {}

def cleanup_tasks():
    """Оставляет только последние 100 задач в памяти."""
    global tasks
    if len(tasks) > 100:
        # Сохраняем порядок вставки (Python 3.7+)
        tasks = dict(list(tasks.items())[-100:])

# === 7. PIPELINE EXECUTOR ===
async def execute_pipeline(job_id: str, stage: str, input_text: str, project_path: Path):
    task = tasks[job_id]
    task["status"] = "running"
    task["progress"] = 5
    task["status_msg"] = "Инициализация..."
    task["started_at"] = datetime.now().isoformat()
    logger.info(f"▶ Starting {stage} | Project: {project_path.name} | Job: {job_id}")

    try:
        from core.router import run_stage

        # Прогресс: 10-30% (контекст + черновик)
        task["progress"] = 15
        task["status_msg"] = "Генерация черновика (draft)..."

        # Прогресс: 30-75% (черновик готов, идёт шлифовка)
        task["progress"] = 35
        task["status_msg"] = "Стратегическая шлифовка (polish)..."

        # Запуск с retry
        def sync_runner():
            return run_stage(stage, input_text, str(project_path))

        result = await run_with_retry(sync_runner, max_retries=3, base_delay=2.0)

        task["progress"] = 80
        task["status_msg"] = "Атомарное сохранение артефакта..."

        # Сохраняем атомарно
        output_file = project_path / "output" / f"{stage}_final.md"
        atomic_write(output_file, result)

        task["progress"] = 100
        task["status"] = "completed"
        task["status_msg"] = "Готово"
        task["result_preview"] = result[:600] + ("..." if len(result) > 600 else "")
        task["completed_at"] = datetime.now().isoformat()
        logger.info(f"✅ Completed {stage} | Job: {job_id}")

    except Exception as e:
        task["status"] = "failed"
        task["status_msg"] = f"Ошибка: {str(e)}"
        task["error"] = str(e)
        task["progress"] = 0
        logger.error(f"❌ Failed {stage} | Job: {job_id} | {e}")
    finally:
        cleanup_tasks()

# === 8. FASTAPI APP ===
app = FastAPI(title="PMF Pipeline Web", version="3.0")

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    projects = sorted([d.name for d in PROJECTS_ROOT.iterdir() if d.is_dir()])
    stages = list(cfg.get("routing", {}).keys())
    
    return f"""
    <!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PMF Pipeline v3.0</title>
    <style>
        :root {{ --bg:#0b1120; --card:#111827; --text:#e2e8f0; --muted:#94a3b8; --accent:#3b82f6; --ok:#10b981; --warn:#f59e0b; --err:#ef4444; }}
        * {{ box-sizing: border-box; }}
        body {{ font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; line-height: 1.5; }}
        .wrap {{ max-width: 1100px; margin: 0 auto; }}
        h1, h2 {{ margin: 0 0 12px; color: #fff; }}
        .card {{ background: var(--card); border: 1px solid #1f2937; border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
        form {{ display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }}
        form > *:last-child {{ grid-column: 1 / -1; display: flex; gap: 10px; align-items: center; }}
        input, select, textarea, button {{ padding: 10px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #fff; font-size: 14px; }}
        button {{ background: var(--accent); border: none; cursor: pointer; font-weight: 600; transition: 0.2s; }}
        button:hover {{ opacity: 0.9; }}
        button:disabled {{ background: #334155; cursor: not-allowed; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th, td {{ padding: 10px; text-align: left; border-bottom: 1px solid #1f2937; }}
        th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; }}
        .bar {{ height: 6px; background: #1f2937; border-radius: 3px; overflow: hidden; width: 100%; }}
        .fill {{ height: 100%; transition: width 0.4s ease; background: var(--muted); }}
        .fill.running {{ background: var(--warn); animation: pulse 1.5s infinite; }}
        .fill.done {{ background: var(--ok); }}
        .fill.fail {{ background: var(--err); }}
        @keyframes pulse {{ 0%, 100% {{ opacity: 0.6; }} 50% {{ opacity: 1; }} }}
        .tag {{ padding: 3px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; text-transform: uppercase; }}
        .t-queued {{ background: #334155; color: #94a3b8; }}
        .t-running {{ background: rgba(245,158,11,0.2); color: #fbbf24; }}
        .t-done {{ background: rgba(16,185,129,0.2); color: #34d399; }}
        .t-fail {{ background: rgba(239,68,68,0.2); color: #f87171; }}
        a {{ color: var(--accent); text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .status-msg {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
        #tokenBar {{ display: flex; gap: 8px; margin-bottom: 10px; align-items: center; }}
    </style></head><body><div class="wrap">
        <h1>🚀 PMF Pipeline <span style="font-size:0.6em;color:#64748b">v3.0</span></h1>
        
        <div class="card">
            <div id="tokenBar">
                <span>🔑 API Token:</span>
                <input type="password" id="apiToken" placeholder="Введите токен" style="flex:1; max-width:300px;">
                <button onclick="saveToken()" style="padding:8px 12px;">💾</button>
            </div>
            <form id="runForm">
                <select name="project" id="projSelect" required><option value="">Проект...</option>{"".join(f"<option value='{p}'>{p}</option>" for p in projects)}</select>
                <select name="stage" id="stageSelect" required><option value="">Этап...</option>{"".join(f"<option value='{s}'>{s}</option>" for s in stages)}</select>
                <textarea name="input" id="inputText" rows="3" placeholder="Контекст или задача..." required></textarea>
                <button type="submit" id="submitBtn">▶ Запустить этап</button>
            </form>
            <p id="formStatus" class="status-msg"></p>
        </div>

        <div class="card">
            <h2>📊 Очередь задач</h2>
            <table><thead><tr><th style="width:120px">ID</th><th>Проект / Этап</th><th>Статус</th><th style="width:220px">Прогресс</th><th>Действие</th></tr></thead>
            <tbody id="tBody"></tbody></table>
        </div>
    </div>
    <script>
        const $ = id => document.getElementById(id);
        const tokenKey = 'pmf_v3_token';
        let token = localStorage.getItem(tokenKey) || '';
        $('apiToken').value = token;
        const headers = () => ({ 'Authorization': `Bearer ${token}` });

        function saveToken() {
            token = $('apiToken').value.trim();
            if(!token) return alert('Токен обязателен');
            localStorage.setItem(tokenKey, token);
            loadTasks();
        }

        $('runForm').onsubmit = async e => {
            e.preventDefault();
            if(!token) return alert('Введите API Token в поле выше');
            const btn = $('submitBtn'), status = $('formStatus');
            btn.disabled = true; status.textContent = '⏳ Отправка...';
            try {
                const fd = new FormData(e.target);
                const res = await fetch('/api/run', { method:'POST', headers: { ...headers(), 'Content-Type':'application/x-www-form-urlencoded' }, body: new URLSearchParams(fd) });
                if(!res.ok) throw new Error(await res.text());
                const d = await res.json();
                status.textContent = `✅ Queued: ${d.job_id}`;
                e.target.reset(); loadTasks();
            } catch(err) { status.textContent = `❌ ${err.message}`; }
            finally { btn.disabled = false; }
        };

        async function loadTasks() {
            if(!token) return;
            try {
                const res = await fetch('/api/jobs', { headers: headers() });
                if(!res.ok) return;
                const jobs = await res.json();
                $('tBody').innerHTML = jobs.map(j => {
                    const cls = j.status === 'completed' ? 't-done' : j.status === 'failed' ? 't-fail' : j.status === 'running' ? 't-running' : 't-queued';
                    const fillCls = j.status === 'completed' ? 'done' : j.status === 'failed' ? 'fail' : j.status === 'running' ? 'running' : '';
                    return `<tr>
                        <td><code title="${j.id}">${j.id.slice(0,8)}...</code></td>
                        <td>${j.project}<br><small style="color:#64748b">${j.stage}</small></td>
                        <td><span class="tag ${cls}">${j.status}</span><div class="status-msg">${j.status_msg||''}</div></td>
                        <td><div class="bar"><div class="fill ${fillCls}" style="width:${j.progress}%"></div></div><small>${j.progress}%</small></td>
                        <td>${j.status==='completed'?`<a href="/download/${j.project}/${j.stage}_final.md">📥 Скачать</a>`:''}</td>
                    </tr>`;
                }).join('') || '<tr><td colspan="5" style="text-align:center;color:#64748b">Нет задач</td></tr>';
            } catch(e) { console.error(e); }
        }
        setInterval(loadTasks, 2000);
        loadTasks();
    </script></body></html>"""

# === 9. API ENDPOINTS ===
@app.post("/api/run", dependencies=[Depends(verify_token)])
async def queue_task(project: str = Form(...), stage: str = Form(...), input: str = Form(...), background_tasks: BackgroundTasks = BackgroundTasks()):
    proj_path = PROJECTS_ROOT / project
    if not proj_path.exists(): raise HTTPException(404, "Проект не найден")
    if stage not in cfg.get("routing", {}): raise HTTPException(400, "Неизвестный этап")

    job_id = str(uuid.uuid4())
    tasks[job_id] = {"id": job_id, "status": "queued", "progress": 0, "stage": stage, "project": project, "status_msg": "В очереди", "result_preview": None, "error": None}
    background_tasks.add_task(execute_pipeline, job_id, stage, input, proj_path)
    return JSONResponse({"job_id": job_id, "status": "queued"})

@app.get("/api/jobs", dependencies=[Depends(verify_token)])
async def get_jobs():
    return list(tasks.values())

@app.get("/download/{project}/{filename}", dependencies=[Depends(verify_token)])
async def download_file(project: str, filename: str):
    path = PROJECTS_ROOT / project / "output" / filename
    if not path.exists(): raise HTTPException(404, "Файл не найден")
    return FileResponse(path, filename=filename)

@app.get("/balance")
async def check_balance():
    try:
        headers = {"Authorization": f"Bearer {cfg['llm']['openrouter']['api_key']}"}
        r = requests.get(f"{cfg['llm']['openrouter']['base_url']}/credits", headers=headers, timeout=5)
        r.raise_for_status()
        balance = r.json().get("total_credits", 0)
        return JSONResponse({"balance_usd": balance, "alert": balance < float(cfg['llm']['openrouter'].get("balance_threshold_usd", 1.0))})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# Запуск: uvicorn entrypoints.web_ui:app --host 127.0.0.1 --port 8000 --reload
```

---

## 🔑 Ключевые фичи в коде

| Фича | Где реализовано | Почему так |
|------|----------------|------------|
| **Retry LLM** | `run_with_retry()` | Ловит любые ошибки сети/API, ждёт `2^N` сек,重试 до 3 раз. Не блокирует event loop (`asyncio.to_thread`) |
| **Atomic Write** | `atomic_write()` | `tempfile.mkstemp` → `os.replace`. Гарантирует, что файл не будет повреждён при падении процесса или обрыве диска |
| **Progress UI** | JS `setInterval` + CSS `@keyframes` | Поллинг `/api/jobs` каждые 2 сек. Прогресс симулируется по этапам пайплайна (реальный прогресс из `router` требует callback-интерфейса) |
| **Memory Cleanup** | `cleanup_tasks()` | Автоматически оставляет только последние 100 задач. Предотвращает утечку RAM при долгой работе |
| **Token UX** | `localStorage` + input field | Токен сохраняется в браузере. Поле скрыто (`password`). Не требует логина, но защищает эндпоинты |
| **Journald Ready** | `logging.StreamHandler(sys.stdout)` | Systemd перехватывает stdout/stderr → автоматически пишет в journald. Ротация на уровне OS |

---

## 🚀 Как запустить

1. Убедись, что в `config.yaml` есть:
   ```yaml
   web_api_token: "твой_секретный_ключ"
   ```
2. Запусти сервис:
   ```bash
   uvicorn entrypoints.web_ui:app --host 127.0.0.1 --port 8000 --workers 2
   ```
3. Открой `http://127.0.0.1:8000`, введи токен → дашборд готов.

Вот полная, готовая к вставке версия `entrypoints/web_ui.py` с интегрированной функцией **«Обработка нового контекста»**. Она автоматически отслеживает, что добавилось с последнего запуска, чистит шум, выделяет ядро и обновляет документацию проекта.

```python
# entrypoints/web_ui.py
import os
import sys
import uuid
import asyncio
import logging
import tempfile
import hashlib
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

import yaml
import requests
from fastapi import FastAPI, Form, BackgroundTasks, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# === 1. CONFIG & PATHS ===
BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config.yaml"
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

PROJECTS_ROOT = (BASE_DIR / cfg.get("projects_root", "projects")).resolve()
PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)

# === 2. LOGGING ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WEB] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("pmf-web")

# === 3. SECURITY ===
security = HTTPBearer()
API_TOKEN = cfg.get("web_api_token", os.getenv("PMF_WEB_TOKEN", "change-me-please"))

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid API token")
    return True

# === 4. ATOMIC WRITE ===
def atomic_write(filepath: Path, content: str):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=filepath.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(filepath))
    except Exception:
        if os.path.exists(tmp_path): os.unlink(tmp_path)
        raise

# === 5. RETRY LOGIC ===
async def run_with_retry(func, args=(), kwargs=None, max_retries=3, base_delay=2.0):
    kwargs = kwargs or {}
    for attempt in range(1, max_retries + 1):
        try:
            return await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else await asyncio.to_thread(func, *args, **kwargs)
        except Exception as e:
            if attempt == max_retries: raise
            await asyncio.sleep(base_delay ** (attempt - 1))

# === 6. CONTEXT TRACKER (отслеживает, что уже обработано) ===
def get_context_state(project_path: Path) -> dict:
    state_file = project_path / ".context_state.json"
    if state_file.exists():
        import json; return json.loads(state_file.read_text(encoding="utf-8"))
    return {"group_lines": 0, "inbox_hash": "", "last_updated": None}

def save_context_state(project_path: Path, state: dict):
    state["last_updated"] = datetime.now().isoformat()
    import json; atomic_write(project_path / ".context_state.json", json.dumps(state, indent=2))

def read_new_context(project_path: Path, mode: str) -> str:
    """Считывает только то, что добавилось с последнего запуска."""
    state = get_context_state(project_path)
    text = ""
    
    if mode in ["group", "all"]:
        ctx_file = project_path / "group_context.md"
        if ctx_file.exists():
            lines = ctx_file.read_text(encoding="utf-8").splitlines()
            new_lines = lines[state.get("group_lines", 0):]
            text += f"[GROUP LOGS]\n" + "\n".join(new_lines) + "\n\n"
            state["group_lines"] = len(lines)
            
    if mode in ["inbox", "all"]:
        inbox_dir = project_path / "inbox"
        if inbox_dir.exists():
            files = sorted(inbox_dir.iterdir(), key=lambda x: x.stat().st_mtime)
            # Простой хеш-чек: если файлы не менялись, пропускаем
            current_hash = hashlib.md5(str([f.stat().st_mtime for f in files]).encode()).hexdigest()
            if current_hash != state.get("inbox_hash"):
                for f in files:
                    text += f"[INBOX: {f.name}]\n{f.read_text(encoding='utf-8')}\n\n"
                state["inbox_hash"] = current_hash

    return text.strip(), state

# === 7. TASK STATE ===
tasks: Dict[str, dict] = {}
def cleanup_tasks():
    global tasks
    if len(tasks) > 150:
        tasks = dict(list(tasks.items())[-150:])

# === 8. PIPELINE EXECUTORS ===
async def execute_pipeline(job_id: str, stage: str, input_text: str, project_path: Path):
    task = tasks[job_id]
    task.update(status="running", progress=10, status_msg="Инициализация...")
    try:
        from core.router import run_stage
        def sync_runner(): return run_stage(stage, input_text, str(project_path))
        result = await run_with_retry(sync_runner, max_retries=3, base_delay=2.0)
        atomic_write(project_path / "output" / f"{stage}_final.md", result)
        task.update(status="completed", progress=100, status_msg="Готово", result_preview=result[:400])
    except Exception as e:
        task.update(status="failed", progress=0, status_msg=f"Ошибка: {e}", error=str(e))
    finally: cleanup_tasks()

async def execute_context_processing(job_id: str, project: str, mode: str):
    task = tasks[job_id]
    task.update(status="running", progress=15, status_msg="Чтение сырого контекста...")
    
    proj_path = PROJECTS_ROOT / project
    raw_context, state = read_new_context(proj_path, mode)
    if not raw_context:
        task.update(status="completed", progress=100, status_msg="✅ Новых данных нет. Нечего обрабатывать.")
        save_context_state(proj_path, state)
        return

    task.update(progress=30, status_msg="Очистка от шума и выделение ядра (LLM)...")
    prompt = f"""Ты — старший продуктовый аналитик. Твоя задача: очистить сырой лог от мусора, повторов и оффтопа.
Оставь ТОЛЬКО ядро:
1. Подтверждённые/опровергнутые гипотезы
2. Чёткие инсайты о болях, поведении, мотивах пользователей
3. Новые требования или изменения в метриках
4. Идеи, требующие проверки

Формат: Markdown. Заголовки H2/H3, списки, без воды. Если ничего нового не найдено → верни "✅ Шум отфильтрован. Новых инсайтов не обнаружено."

Сырой контекст:
{raw_context[:8000]}  # лимит на случай огромных логов
"""
    model = cfg["llm"]["openrouter"]["draft_model"]
    headers = {"Authorization": f"Bearer {cfg['llm']['openrouter']['api_key']}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 2000, "temperature": 0.2}
    
    def call_llm():
        r = requests.post(f"{cfg['llm']['openrouter']['base_url']}/chat/completions", headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    task.update(progress=60, status_msg="Генерация очищенной документации...")
    cleaned = await run_with_retry(call_llm, max_retries=2, base_delay=3.0)

    task.update(progress=85, status_msg="Сохранение артефакта...")
    digest_path = proj_path / "docs" / "context_digest.md"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Добавляем временную метку и сохраняем атомарно
    header = f"## 📅 {datetime.now().strftime('%Y-%m-%d %H:%M')} | Режим: {mode}\n\n"
    atomic_write(digest_path, header + cleaned + "\n\n---\n" + (digest_path.read_text(encoding="utf-8") if digest_path.exists() else ""))

    save_context_state(proj_path, state)
    task.update(status="completed", progress=100, status_msg="✅ Контекст обработан и сохранён в docs/context_digest.md")

# === 9. FASTAPI APP ===
app = FastAPI(title="PMF Pipeline Web", version="3.0")

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    projects = sorted([d.name for d in PROJECTS_ROOT.iterdir() if d.is_dir()])
    stages = list(cfg.get("routing", {}).keys())
    
    return f"""
    <!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PMF Pipeline v3.0</title>
    <style>
        :root {{ --bg:#0b1120; --card:#111827; --text:#e2e8f0; --muted:#94a3b8; --accent:#3b82f6; --ok:#10b981; --warn:#f59e0b; --err:#ef4444; }}
        * {{ box-sizing: border-box; }}
        body {{ font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; line-height: 1.5; }}
        .wrap {{ max-width: 1200px; margin: 0 auto; }}
        h1, h2 {{ margin: 0 0 12px; color: #fff; }}
        .card {{ background: var(--card); border: 1px solid #1f2937; border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
        .grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }}
        form {{ display: flex; flex-direction: column; gap: 10px; }}
        input, select, textarea, button {{ padding: 10px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #fff; font-size: 14px; }}
        button {{ background: var(--accent); border: none; cursor: pointer; font-weight: 600; transition: 0.2s; }}
        button:hover {{ opacity: 0.9; }} button:disabled {{ background: #334155; cursor: not-allowed; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th, td {{ padding: 10px; text-align: left; border-bottom: 1px solid #1f2937; }}
        th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; }}
        .bar {{ height: 6px; background: #1f2937; border-radius: 3px; overflow: hidden; width: 100%; }}
        .fill {{ height: 100%; transition: width 0.4s ease; background: var(--muted); }}
        .fill.running {{ background: var(--warn); animation: pulse 1.5s infinite; }}
        .fill.done {{ background: var(--ok); }} .fill.fail {{ background: var(--err); }}
        @keyframes pulse {{ 0%,100%{{opacity:0.6}} 50%{{opacity:1}} }}
        .tag {{ padding: 3px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; text-transform: uppercase; }}
        .t-queued {{ background: #334155; color: #94a3b8; }} .t-running {{ background: rgba(245,158,11,0.2); color: #fbbf24; }}
        .t-done {{ background: rgba(16,185,129,0.2); color: #34d399; }} .t-fail {{ background: rgba(239,68,68,0.2); color: #f87171; }}
        a {{ color: var(--accent); text-decoration: none; }} a:hover {{ text-decoration: underline; }}
        #tokenBar {{ display: flex; gap: 8px; margin-bottom: 10px; align-items: center; flex-wrap: wrap; }}
    </style></head><body><div class="wrap">
        <h1>🚀 PMF Pipeline <span style="font-size:0.6em;color:#64748b">v3.0</span></h1>
        <div class="card">
            <div id="tokenBar">
                <span>🔑 API Token:</span>
                <input type="password" id="apiToken" placeholder="Введите токен" style="flex:1; max-width:300px;">
                <button onclick="saveToken()" style="padding:8px 12px;">💾 Сохранить</button>
                <span id="tokenStatus" style="font-size:12px;color:#64748b;"></span>
            </div>
        </div>
        <div class="grid">
            <div class="card">
                <h2>▶ Запуск этапа (Draft→Polish)</h2>
                <form id="runForm">
                    <select name="project" id="projSelect1" required><option value="">Проект...</option>{"".join(f"<option value='{p}'>{p}</option>" for p in projects)}</select>
                    <select name="stage" required>{"".join(f"<option value='{s}'>{s}</option>" for s in stages)}</select>
                    <textarea name="input" rows="3" placeholder="Контекст или задача..." required></textarea>
                    <button type="submit" id="btnRun">▶ Запустить</button>
                </form>
            </div>
            <div class="card">
                <h2>🧹 Обработать новый контекст</h2>
                <form id="ctxForm">
                    <select name="project" id="projSelect2" required><option value="">Проект...</option>{"".join(f"<option value='{p}'>{p}</option>" for p in projects)}</select>
                    <select name="mode" required>
                        <option value="all">Все источники (Group + Inbox)</option>
                        <option value="group">Только логи группы</option>
                        <option value="inbox">Только личные заметки</option>
                    </select>
                    <button type="submit" id="btnCtx">🔍 Очистить & Обновить документацию</button>
                </form>
                <p id="ctxStatus" style="margin-top:10px;font-size:12px;color:#94a3b8;"></p>
            </div>
        </div>
        <div class="card">
            <h2>📊 Очередь задач</h2>
            <table><thead><tr><th style="width:100px">ID</th><th>Тип / Проект</th><th>Статус</th><th style="width:200px">Прогресс</th><th>Действие</th></tr></thead>
            <tbody id="tBody"></tbody></table>
        </div>
    </div>
    <script>
        const $ = id => document.getElementById(id);
        const tokenKey = 'pmf_v3_token';
        let token = localStorage.getItem(tokenKey) || '';
        $('apiToken').value = token;
        const headers = () => ({ 'Authorization': `Bearer ${token}` });

        function saveToken() {
            token = $('apiToken').value.trim();
            if(!token) return alert('Токен обязателен');
            localStorage.setItem(tokenKey, token);
            $('tokenStatus').textContent = '✅ Сохранено в браузере';
            loadTasks();
        }

        async function submitForm(formId, endpoint, btnId, statusId) {
            const form = $(formId), btn = $(btnId), status = $(statusId);
            if(!token) return alert('Введите API Token');
            btn.disabled = true; status.textContent = '⏳ Отправка...';
            try {
                const fd = new FormData(form);
                const res = await fetch(endpoint, { method:'POST', headers: { ...headers(), 'Content-Type':'application/x-www-form-urlencoded' }, body: new URLSearchParams(fd) });
                if(!res.ok) throw new Error(await res.text());
                const d = await res.json();
                status.textContent = `✅ В очереди: ${d.job_id.slice(0,8)}`;
                form.reset(); loadTasks();
            } catch(err) { status.textContent = `❌ ${err.message}`; }
            finally { btn.disabled = false; }
        }

        $('runForm').onsubmit = e => { e.preventDefault(); submitForm('runForm', '/api/run', 'btnRun', null); };
        $('ctxForm').onsubmit = e => { e.preventDefault(); submitForm('ctxForm', '/api/process_context', 'btnCtx', 'ctxStatus'); };

        async function loadTasks() {
            if(!token) return;
            try {
                const res = await fetch('/api/jobs', { headers: headers() });
                if(!res.ok) return;
                const jobs = await res.json();
                $('tBody').innerHTML = jobs.slice(-50).reverse().map(j => {
                    const cls = j.status==='completed'?'t-done':j.status==='failed'?'t-fail':j.status==='running'?'t-running':'t-queued';
                    const fillCls = j.status==='completed'?'done':j.status==='failed'?'fail':j.status==='running'?'running':'';
                    return `<tr>
                        <td><code title="${j.id}">${j.id.slice(0,8)}</code></td>
                        <td>${j.type || 'Этап'}<br><small style="color:#64748b">${j.project} ${j.stage || ''}</small></td>
                        <td><span class="tag ${cls}">${j.status}</span><div style="font-size:11px;color:#94a3b8;margin-top:2px">${j.status_msg||''}</div></td>
                        <td><div class="bar"><div class="fill ${fillCls}" style="width:${j.progress}%"></div></div><small>${j.progress}%</small></td>
                        <td>${j.status==='completed' && j.file?`<a href="${j.file}">📥 Скачать</a>`:''}</td>
                    </tr>`;
                }).join('') || '<tr><td colspan="5" style="text-align:center;color:#64748b">Нет задач</td></tr>';
            } catch(e) { console.error(e); }
        }
        setInterval(loadTasks, 2000); loadTasks();
    </script></body></html>"""

# === 10. API ENDPOINTS ===
@app.post("/api/run", dependencies=[Depends(verify_token)])
async def queue_task(project: str = Form(...), stage: str = Form(...), input: str = Form(...), background_tasks: BackgroundTasks = BackgroundTasks()):
    proj_path = PROJECTS_ROOT / project
    if not proj_path.exists(): raise HTTPException(404, "Проект не найден")
    if stage not in cfg.get("routing", {}): raise HTTPException(400, "Неизвестный этап")
    job_id = str(uuid.uuid4())
    tasks[job_id] = {"id": job_id, "type": "pipeline", "status": "queued", "progress": 0, "stage": stage, "project": project, "status_msg": "В очереди", "file": None}
    background_tasks.add_task(execute_pipeline, job_id, stage, input, proj_path)
    return JSONResponse({"job_id": job_id, "status": "queued"})

@app.post("/api/process_context", dependencies=[Depends(verify_token)])
async def queue_context_process(project: str = Form(...), mode: str = Form("all"), background_tasks: BackgroundTasks = BackgroundTasks()):
    proj_path = PROJECTS_ROOT / project
    if not proj_path.exists(): raise HTTPException(404, "Проект не найден")
    if mode not in ["group", "inbox", "all"]: raise HTTPException(400, "Неверный режим")
    
    job_id = str(uuid.uuid4())
    tasks[job_id] = {"id": job_id, "type": "context_cleanup", "status": "queued", "progress": 0, "mode": mode, "project": project, "status_msg": "В очереди", "file": f"/download/{project}/docs/context_digest.md"}
    background_tasks.add_task(execute_context_processing, job_id, project, mode)
    return JSONResponse({"job_id": job_id, "status": "queued"})

@app.get("/api/jobs", dependencies=[Depends(verify_token)])
async def get_jobs(): return list(tasks.values())

@app.get("/download/{project}/{filename}", dependencies=[Depends(verify_token)])
async def download_file(project: str, filename: str):
    path = PROJECTS_ROOT / project / filename.lstrip("/")
    if not path.exists(): raise HTTPException(404, "Файл не найден")
    return FileResponse(path, filename=filename.split("/")[-1])

@app.get("/balance")
async def check_balance():
    try:
        headers = {"Authorization": f"Bearer {cfg['llm']['openrouter']['api_key']}"}
        r = requests.get(f"{cfg['llm']['openrouter']['base_url']}/credits", headers=headers, timeout=5)
        r.raise_for_status()
        balance = r.json().get("total_credits", 0)
        return JSONResponse({"balance_usd": balance, "alert": balance < float(cfg['llm']['openrouter'].get("balance_threshold_usd", 1.0))})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# Запуск: uvicorn entrypoints.web_ui:app --host 127.0.0.1 --port 8000 --reload
```

---

## 🔍 Как это работает «под капотом»

| Компонент | Логика |
|-----------|--------|
| **Трекер состояния** | `.context_state.json` в папке проекта хранит `group_lines` (сколько строк уже обработано) и `inbox_hash` (хеш времён файлов). При повторном запуске читаются **только новые** данные. |
| **Фильтрация шума** | Промпт явно запрещает LLM сохранять мусор. Температура `0.2` гарантирует строгость. Лимит `8000` символов на вход защищает от переполнения контекста. |
| **Сохранение** | Результат пишется в `docs/context_digest.md` с временной меткой. Старые записи не удаляются, а сдвигаются вниз → полная история очистки доступна для аудита. |
| **Интеграция в UI** | Отдельная карточка с выбором проекта и режима (`all/group/inbox`). Задача попадает в общую таблицу, прогресс обновляется каждые 2 сек. |
| **Безопасность** | Атомарная запись (`tempfile` + `os.replace`), retry при сетевых сбоях, трекинг в памяти с автоочисткой (>150 задач). |

---

## 🚀 Как использовать
1. Открой Web UI → выбери проект → нажми `🔍 Очистить & Обновить документацию`
2. Выбери режим:
   - `Все источники` → читает `group_context.md` + `inbox/`
   - `Только логи группы` → только чат/батчи
   - `Только личные заметки` → только файлы из `inbox/`
3. Дождись завершения → ссылка на скачивание появится в таблице задач
4. Файл `docs/context_digest.md` автоматически пополняется чистым ядром контекста. Готов к использованию в следующих этапах PMF.

# 📊 Визуализация очищенного контекста: концепция + готовый код

Вот как может выглядеть дашборд после обработки контекста. Всё встроено в существующий `web_ui.py`, сохраняет тёмную тему и работает без внешних библиотек.

---

## 🎯 Что визуализируем

| Элемент | Зачем | Тип визуализации |
|---------|-------|-----------------|
| **Гипотезы** | Отслеживать статус: `новая → в проверке → подтверждена/опровергнута` | Карточки с бейджами + прогресс-бар |
| **Инсайты** | Видеть паттерны в болях, мотивах, поведении | Теги + частотная облако + таймлайн |
| **D/V/F матрица** | Быстро оценить баланс желаний, экономики и реализуемости | Радар-чарт (чистый SVG/JS) |
| **История очистки** | Понимать, как менялся контекст во времени | Вертикальная лента с датами |
| **Метрики** | Отслеживать Sean Ellis, retention, когорты | Мини-графики (sparklines) + таблицы |

---

## 🖼️ Макет карточки проекта (добавить в `dashboard()`)

```html
<!-- Вставить после карточек запуска -->
<div class="card" id="projectViz" style="display:none">
    <h2>📈 Визуализация: <span id="vizProjectName"></span></h2>
    
    <!-- Вкладки -->
    <div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">
        <button class="tab-btn active" data-tab="hypotheses">🎯 Гипотезы</button>
        <button class="tab-btn" data-tab="insights">💡 Инсайты</button>
        <button class="tab-btn" data-tab="dvf">⚖️ D/V/F</button>
        <button class="tab-btn" data-tab="timeline">📅 История</button>
    </div>
    
    <!-- Контент вкладок -->
    <div id="tab-hypotheses" class="tab-content active">
        <div id="hypothesesGrid" class="grid"></div>
    </div>
    <div id="tab-insights" class="tab-content" style="display:none">
        <div id="insightsCloud" style="min-height:150px;margin-bottom:16px"></div>
        <div id="insightsList"></div>
    </div>
    <div id="tab-dvf" class="tab-content" style="display:none;text-align:center">
        <svg id="radarChart" width="300" height="300"></svg>
        <p id="dvfSummary" style="margin-top:12px;font-size:14px;color:#94a3b8"></p>
    </div>
    <div id="tab-timeline" class="tab-content" style="display:none">
        <div id="timelineList"></div>
    </div>
    
    <!-- Кнопки действий -->
    <div style="margin-top:20px;display:flex;gap:10px;flex-wrap:wrap">
        <button onclick="exportViz('png')">📥 Экспорт PNG</button>
        <button onclick="exportViz('md')">📄 Экспорт в Markdown</button>
        <button onclick="refreshViz()">🔄 Обновить данные</button>
    </div>
</div>
```

---

## 🎨 CSS-стилизация (добавить в `<style>`)

```css
/* Вкладки */
.tab-btn {
    padding: 8px 16px;
    border: 1px solid #334155;
    background: #0f172a;
    color: #94a3b8;
    border-radius: 8px;
    cursor: pointer;
    font-size: 13px;
    transition: 0.2s;
}
.tab-btn.active, .tab-btn:hover {
    background: var(--accent);
    color: #fff;
    border-color: var(--accent);
}
.tab-content { animation: fadeIn 0.3s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }

/* Карточки гипотез */
.hypo-card {
    background: #0f172a;
    border: 1px solid #1f2937;
    border-radius: 10px;
    padding: 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
}
.hypo-title { font-weight: 600; font-size: 14px; }
.hypo-status {
    display: inline-flex;
    padding: 3px 10px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
}
.status-new { background: #334155; color: #94a3b8; }
.status-testing { background: rgba(245,158,11,0.2); color: #fbbf24; }
.status-validated { background: rgba(16,185,129,0.2); color: #34d399; }
.status-rejected { background: rgba(239,68,68,0.2); color: #f87171; }

/* Облако тегов */
.tag-cloud { display: flex; flex-wrap: wrap; gap: 8px; }
.tag {
    padding: 4px 12px;
    background: #1e293b;
    border-radius: 20px;
    font-size: 12px;
    color: #e2e8f0;
    border: 1px solid #334155;
    transition: 0.2s;
}
.tag:hover { background: var(--accent); border-color: var(--accent); }
.tag.freq-1 { font-size: 12px; opacity: 0.7; }
.tag.freq-2 { font-size: 14px; }
.tag.freq-3 { font-size: 16px; font-weight: 500; }
.tag.freq-4 { font-size: 18px; font-weight: 600; color: #fff; }

/* Радар-чарт */
.radar-axis { stroke: #334155; stroke-width: 1; }
.radar-grid { stroke: #1f2937; stroke-dasharray: 4; }
.radar-point { fill: var(--accent); stroke: #fff; stroke-width: 2; }
.radar-label { font-size: 11px; fill: #94a3b8; text-anchor: middle; }

/* Таймлайн */
.timeline-item {
    display: flex;
    gap: 12px;
    padding: 12px 0;
    border-bottom: 1px solid #1f2937;
}
.timeline-date {
    min-width: 100px;
    font-size: 12px;
    color: #64748b;
    font-family: monospace;
}
.timeline-content { flex: 1; }
.timeline-badge {
    width: 10px; height: 10px;
    border-radius: 50%;
    background: var(--accent);
    margin-right: 8px;
    display: inline-block;
}
```

---

## ⚙️ JavaScript-логика (добавить в `<script>`)

```javascript
// === Вкладки ===
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.onclick = () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.style.display = 'none');
        btn.classList.add('active');
        document.getElementById(`tab-${btn.dataset.tab}`).style.display = 'block';
    };
});

// === Загрузка визуализации ===
async function loadViz(project) {
    if(!token) return;
    $('#projectViz').style.display = 'block';
    $('#vizProjectName').textContent = project;
    
    try {
        // Загружаем очищенный контекст
        const res = await fetch(`/download/${project}/docs/context_digest.md`, { headers: headers() });
        if(!res.ok) { $('#projectViz').innerHTML += '<p style="color:#94a3b8">📭 Нет обработанного контекста. Запусти очистку.</p>'; return; }
        const text = await res.text();
        
        // Парсим Markdown → JSON (упрощённо)
        const data = parseDigestMarkdown(text);
        
        // Рендерим вкладки
        renderHypotheses(data.hypotheses || []);
        renderInsights(data.insights || []);
        renderRadar(data.dvf || {d:5,v:5,f:5});
        renderTimeline(data.history || []);
        
    } catch(e) {
        console.error('Viz load error:', e);
        $('#projectViz').innerHTML += `<p style="color:#ef4444">❌ Ошибка загрузки: ${e.message}</p>`;
    }
}

// === Парсер digest.md (упрощённый) ===
function parseDigestMarkdown(md) {
    const result = { hypotheses: [], insights: [], dvf: {}, history: [] };
    const lines = md.split('\n');
    let section = null;
    
    for(const line of lines) {
        if(line.startsWith('## 🎯 Гипотезы')) section = 'hypotheses';
        else if(line.startsWith('## 💡 Инсайты')) section = 'insights';
        else if(line.startsWith('## ⚖️ D/V/F')) section = 'dvf';
        else if(line.startsWith('## 📅')) section = 'history';
        else if(line.trim().startsWith('- [') && section === 'hypotheses') {
            // Парсим: - [СТАТУС] Текст → { status, text }
            const match = line.match(/\[([^\]]+)\]\s*(.+)/);
            if(match) result.hypotheses.push({ status: match[1].toLowerCase(), text: match[2] });
        }
        else if(line.trim().startsWith('• ') && section === 'insights') {
            result.insights.push(line.replace('• ', '').trim());
        }
        else if(line.includes('D:') && section === 'dvf') {
            const nums = line.match(/(\d+)/g);
            if(nums?.length >= 3) result.dvf = { d: +nums[0], v: +nums[1], f: +nums[2] };
        }
        else if(section === 'history' && line.startsWith('### ')) {
            result.history.push({ date: line.replace('### ', '').trim(), items: [] });
        }
    }
    return result;
}

// === Рендер гипотез ===
function renderHypotheses(list) {
    const grid = $('#hypothesesGrid');
    if(!list.length) { grid.innerHTML = '<p style="color:#64748b">Нет гипотез</p>'; return; }
    grid.innerHTML = list.map(h => `
        <div class="hypo-card">
            <div class="hypo-title">${h.text}</div>
            <span class="hypo-status status-${h.status}">${h.status}</span>
            <div style="font-size:12px;color:#94a3b8">Нажми для деталей →</div>
        </div>
    `).join('');
}

// === Рендер инсайтов + облако тегов ===
function renderInsights(list) {
    // Частотный анализ для облака
    const freq = {};
    list.forEach(t => t.split(/\s+/).forEach(w => { if(w.length>3) freq[w] = (freq[w]||0)+1; }));
    const sorted = Object.entries(freq).sort((a,b)=>b[1]-a[1]).slice(0,20);
    
    $('#insightsCloud').innerHTML = `<div class="tag-cloud">${sorted.map(([w,c])=>`<span class="tag freq-${Math.min(4,Math.ceil(c/2))}" title="${c} упом.">${w}</span>`).join('')}</div>`;
    $('#insightsList').innerHTML = list.map(i=>`<div style="padding:8px 0;border-bottom:1px solid #1f2937">• ${i}</div>`).join('');
}

// === Радар-чарт для D/V/F (чистый SVG) ===
function renderRadar({d,v,f}) {
    const svg = $('#radarChart');
    const cx=150, cy=150, r=100;
    const axes = [
        {label:'Desirability', value:d, angle: -Math.PI/2},
        {label:'Viability', value:v, angle: -Math.PI/2 + 2*Math.PI/3},
        {label:'Feasibility', value:f, angle: -Math.PI/2 + 4*Math.PI/3}
    ];
    
    let path = '';
    axes.forEach((ax,i) => {
        const x = cx + r * 0.6 * ax.value/10 * Math.cos(ax.angle);
        const y = cy + r * 0.6 * ax.value/10 * Math.sin(ax.angle);
        path += (i===0?'M':'L') + `${x},${y}`;
    });
    path += 'Z';
    
    svg.innerHTML = `
        <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#1f2937"/>
        ${[20,40,60,80,100].map(p=>`<circle cx="${cx}" cy="${cy}" r="${r*p/100}" fill="none" class="radar-grid"/>`).join('')}
        ${axes.map(ax=>`
            <line x1="${cx}" y1="${cy}" x2="${cx+r*Math.cos(ax.angle)}" y2="${cy+r*Math.sin(ax.angle)}" class="radar-axis"/>
            <text x="${cx+(r+20)*Math.cos(ax.angle)}" y="${cy+(r+20)*Math.sin(ax.angle)}" class="radar-label">${ax.label}</text>
        `).join('')}
        <path d="${path}" fill="rgba(59,130,246,0.2)" stroke="#3b82f6" stroke-width="2"/>
        ${axes.map(ax=>{
            const x = cx + r * 0.6 * ax.value/10 * Math.cos(ax.angle);
            const y = cy + r * 0.6 * ax.value/10 * Math.sin(ax.angle);
            return `<circle cx="${x}" cy="${y}" r="4" class="radar-point"/><text x="${x}" y="${y-8}" font-size="10" fill="#fff" text-anchor="middle">${ax.value}</text>`;
        }).join('')}
    `;
    $('#dvfSummary').textContent = `Баланс: D=${d}/10 • V=${v}/10 • F=${f}/10 | ${d+v+f>=20?'✅ Сбалансировано':'⚠️ Требует внимания'}`;
}

// === Таймлайн истории очистки ===
function renderTimeline(items) {
    $('#timelineList').innerHTML = items.length ? items.map(it=>`
        <div class="timeline-item">
            <div class="timeline-date">${it.date}</div>
            <div class="timeline-content">
                <span class="timeline-badge"></span>
                <div style="font-size:13px">Обработано: ${it.items?.length||0} инсайтов</div>
            </div>
        </div>
    `).join('') : '<p style="color:#64748b">История пуста</p>';
}

// === Экспорт ===
function exportViz(format) {
    if(format === 'md') {
        // Простой экспорт текущего digest + визуальных выводов
        alert('📄 Экспорт в Markdown: функция в разработке');
    } else if(format === 'png') {
        // Можно подключить html2canvas, но для MVP — скриншот
        alert('📸 Сделай скриншот области визуализации');
    }
}

function refreshViz() {
    const project = $('#projSelect1').value || $('#projSelect2').value;
    if(project) loadViz(project);
}

// === Авто-загрузка при выборе проекта ===
['projSelect1','projSelect2'].forEach(id => {
    const el = $(id);
    if(el) el.onchange = () => { if(el.value) loadViz(el.value); };
});
```

---

## 🔗 Интеграция с бэкендом (дополнения к `web_ui.py`)

Добавь эндпоинт для парсинга `context_digest.md` в структурированный JSON (опционально, если не хочешь парсить на клиенте):

```python
# В web_ui.py, после других @app.get
@app.get("/api/viz/{project}", dependencies=[Depends(verify_token)])
async def get_viz_data(project: str):
    """Возвращает парсенные данные из context_digest.md для визуализации."""
    path = PROJECTS_ROOT / project / "docs" / "context_digest.md"
    if not path.exists():
        return JSONResponse({"error": "Нет обработанного контекста"}, status_code=404)
    
    content = path.read_text(encoding="utf-8")
    # Простой парсинг (можно вынести в core/viz_parser.py)
    data = {"hypotheses": [], "insights": [], "dvf": {}, "history": []}
    # ... аналогично JS-парсеру, но на Python ...
    return JSONResponse(content=data)
```

Тогда в JS замени `fetch(/download/...)` на `fetch(/api/viz/${project})` и получишь готовый JSON.

---

## 🎁 Бонус: интерактивные фичи (по желанию)

| Фича | Сложность | Польза |
|------|-----------|--------|
| **Фильтр по статусу гипотез** | Низкая | Быстро скрыть подтверждённые / показать только новые |
| **Клик по гипотезе → модалка с деталями** | Средняя | Просмотр источника, комментариев, связанных инсайтов |
| **Экспорт в Notion / Obsidian** | Средняя | Автоматическая синхронизация документации |
| **Сравнение двух очисток (diff)** | Высокая | Понимать, как эволюционировали инсайты во времени |
| **Авто-обновление при изменении файла** | Средняя | WebSocket или polling на `context_digest.md` |

---

## ✅ Итог

Эта визуализация:
- 🎨 Полностью встраивается в текущий `web_ui.py` (тёмная тема, адаптив)
- ⚡ Работает на чистом JS/SVG — никаких `chart.js`, `d3`, `react`
- 🔗 Читает данные из уже существующего `docs/context_digest.md`
- 🧩 Расширяема: добавляй новые вкладки, типы графиков, экспорт
