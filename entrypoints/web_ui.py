# entrypoints/web_ui.py (v3.0 + v4.0 Addons)
import os, sys, uuid, asyncio, logging, tempfile, json, hashlib, base64, io
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from collections import deque
import yaml, requests
from fastapi import FastAPI, Form, BackgroundTasks, HTTPException, Depends, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# Core imports
from core.task_storage import load_tasks, save_tasks, update_task, cleanup_tasks as cleanup_tasks_persist

# === 1. CONFIG & PATHS ===
BASE_DIR = Path(__file__).resolve().parents[1]
with open(BASE_DIR / "config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

PROJECTS_ROOT = (BASE_DIR / cfg.get("projects_root", "projects")).resolve()
PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)

WEBUI_CFG = cfg.get("webui", {})
FEATURES = WEBUI_CFG.get("features", {})
OWNER_TOKEN = WEBUI_CFG.get("owner_token", os.getenv("PMF_WEB_TOKEN", "change-me-please"))
SHARED_TOKEN = WEBUI_CFG.get("shared_token", "team-view-token")
NOTION_CFG = cfg.get("notion", {})

logging.basicConfig(level=logging.INFO, format="%(asctime)s [WEB] %(levelname)s: %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("pmf-web")

# === 2. APP ===
app = FastAPI(title="PMF Pipeline v4.0")

# === 3. AUTH & ACCESS ===
security = HTTPBearer()

async def get_access(credentials: HTTPAuthorizationCredentials = Depends(security)):
    t = credentials.credentials
    if t not in [OWNER_TOKEN, SHARED_TOKEN]:
        raise HTTPException(401, "Invalid token")
    return t

def require_owner(token: str = Depends(get_access)):
    if token != OWNER_TOKEN:
        raise HTTPException(403, "Owner access required")
    return token

# === 4. V3.0 CORE (Atomic, Retry, Tasks, Pipeline, Context) ===
def atomic_write(filepath: Path, content: str):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=filepath.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f: f.write(content)
        os.replace(tmp, str(filepath))
    except:
        if os.path.exists(tmp): os.unlink(tmp)
        raise

async def run_with_retry(func, args=(), kwargs=None, max_retries=3, base_delay=2.0):
    kwargs = kwargs or {}
    for i in range(1, max_retries+1):
        try: return await func(*args, **kwargs) if asyncio.iscoroutinefunction(func) else await asyncio.to_thread(func, *args, **kwargs)
        except Exception as e:
            if i == max_retries: raise
            await asyncio.sleep(base_delay ** (i-1))

# In-memory cache for tasks (synced with persistent storage)
_tasks_cache: Dict[str, dict] = load_tasks()

def _sync_cache():
    global _tasks_cache
    _tasks_cache = load_tasks()

def _task_set(job_id: str, task_data: dict):
    """Set a task and persist to disk."""
    global _tasks_cache
    _tasks_cache[job_id] = task_data
    save_tasks(_tasks_cache)

def _task_update(job_id: str, **kwargs):
    """Update a task and persist to disk."""
    global _tasks_cache
    if job_id in _tasks_cache:
        _tasks_cache[job_id].update(kwargs)
        save_tasks(_tasks_cache)

def cleanup_tasks():
    cleanup_tasks_persist()
    _sync_cache()

async def execute_pipeline(job_id: str, stage: str, input_text: str, project_path: Path):
    _task_update(job_id, status="running", progress=15, status_msg="Drafting...")
    try:
        from core.router import run_stage
        res = await run_with_retry(run_stage, args=(stage, input_text, str(project_path)), max_retries=2)
        atomic_write(project_path / "output" / f"{stage}_final.md", res)
        _task_update(job_id, status="completed", progress=100, status_msg="Done", result_preview=res[:300])
    except Exception as e:
        _task_update(job_id, status="failed", progress=0, status_msg=f"Error: {e}", error=str(e))
    finally: cleanup_tasks()

async def execute_context_process(job_id: str, project: str, mode: str):
    _task_update(job_id, status="running", progress=20, status_msg="Reading logs...")
    p = PROJECTS_ROOT / project
    raw = ""
    ctx_file = p / "group_context.md"
    if ctx_file.exists(): raw += ctx_file.read_text(encoding="utf-8")[:5000]
    inbox = p / "inbox"
    if inbox.exists(): raw += "\n".join([f.read_text(encoding="utf-8")[:500] for f in sorted(inbox.iterdir(), key=lambda x: x.stat().st_mtime)[-3:]])

    if not raw.strip():
        _task_update(job_id, status="completed", progress=100, status_msg="No new data")
        return

    _task_update(job_id, progress=50, status_msg="Cleaning & Extracting...")
    prompt = f"Очисти лог от мусора. Оставь только ядро: гипотезы, инсайты, метрики, риски. Формат: Markdown.\nКонтекст:\n{raw}"
    res = await run_with_retry(_call_llm, args=(cfg["llm"]["openrouter"]["draft_model"], prompt), max_retries=2)

    digest = p / "docs" / "context_digest.md"
    header = f"## 📅 {datetime.now().strftime('%Y-%m-%d %H:%M')} | Mode: {mode}\n\n"
    atomic_write(digest, header + res + "\n---\n" + (digest.read_text() if digest.exists() else ""))
    _task_update(job_id, status="completed", progress=100, status_msg="Context updated", file=f"/download/{project}/docs/context_digest.md")

def _call_llm(model: str, prompt: str) -> str:
    cfg_llm = cfg["llm"]["openrouter"]
    r = requests.post(f"{cfg_llm['base_url']}/chat/completions",
                      headers={"Authorization": f"Bearer {cfg_llm['api_key']}", "Content-Type": "application/json"},
                      json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1500, "temperature": 0.2}, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

# === 4.5. RAG ENGINE ===
try:
    from core.rag_engine import RAGEngine
    rag_engine = RAGEngine(PROJECTS_ROOT)
    RAG_AVAILABLE = True
    logger.info("RAG engine initialized")
except ImportError as _rag_err:
    rag_engine = None
    RAG_AVAILABLE = False
    logger.warning("RAG engine not available (%s): install chromadb and sentence-transformers", _rag_err)

# === 5. V4.0 ADDONS: Utils ===
activity_path = BASE_DIR / "data" / "activity.json"
activity_path.parent.mkdir(exist_ok=True)
if not activity_path.exists(): activity_path.write_text("[]")

def log_activity(project: str, action: str, details: str = ""):
    logs = json.loads(activity_path.read_text() or "[]")
    logs.append({"time": datetime.now().strftime("%H:%M"), "project": project, "action": action, "details": details})
    activity_path.write_text(json.dumps(logs[-100:], indent=2))

async def chat_with_project(project_path: Path, query: str) -> str:
    ctx = ""
    # Приоритет: context_digest > output/*_final > inbox/*.md > *.md в корне > *.md в подпапках
    def _glob_safe(pattern_fn):
        try: return sorted(pattern_fn())
        except Exception: return []

    candidates = (
        [project_path / "docs/context_digest.md"]
        + _glob_safe(lambda: (project_path / "output").glob("*_final.md"))
        + _glob_safe(lambda: (project_path / "inbox").glob("*.md"))
        + _glob_safe(lambda: project_path.glob("*.md"))
        + _glob_safe(lambda: [f for f in project_path.rglob("*.md")
                               if f.parent != project_path
                               and f.parts[-2] not in ("output", "inbox", "docs")])
    )
    seen = set()
    for f in candidates:
        if f in seen or not f.exists(): continue
        seen.add(f)
        ctx += f"### {f.relative_to(project_path)}\n" + f.read_text(encoding="utf-8")[:3000] + "\n---\n"
        if len(ctx) > 12000: break
    if not ctx: return "📭 Контекст пуст. Добавь документы в папку проекта или запусти этап PMF."
    prompt = f"Ты ассистент проекта. Контекст:\n{ctx}\n\nВопрос: {query}\nОтвечай строго по контексту, до 300 слов. Цитируй источники (имя файла)."
    return await run_with_retry(_call_llm, args=(cfg["llm"]["openrouter"]["draft_model"], prompt), max_retries=2)

# === 6. V4.0 UI: Dashboard ===
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    projs = sorted([d.name for d in PROJECTS_ROOT.iterdir() if d.is_dir()])
    stages = list(cfg.get("routing", {}).keys())

    STAGE_META = {
        "0_setup":               ("Настройка проекта",   "Инициализация: цель, рынок, команда, ресурсы"),
        "1_hypothesis":          ("Гипотезы",            "Формулировка ключевых предположений о проблеме и решении"),
        "2_research":            ("Исследование рынка",  "Анализ конкурентов, трендов, объёма рынка"),
        "3_synthesis":           ("Синтез инсайтов",     "Структурирование данных: паттерны, сегменты, ключевые находки"),
        "4_validation":          ("Валидация гипотез",   "Проверка предположений через данные и первые контакты"),
        "5_interview_prep":      ("Подготовка интервью", "Гайд, вопросы и критерии для custdev-интервью"),
        "6_field":               ("Полевая работа",      "Проведение интервью и сбор обратной связи от пользователей"),
        "7_interview_synthesis": ("Анализ интервью",     "Кластеризация ответов, боли, Jobs-to-be-Done"),
        "8_mvp_launch":          ("Запуск MVP",          "Определение минимального продукта и план первых продаж"),
        "9_metrics":             ("Метрики",             "Настройка воронки, retention, NPS, unit-экономики"),
        "10_iterate":            ("Итерация",            "Приоритизация доработок на основе данных и фидбека"),
    }

    proj_opts = '<option value="">Проект —</option>' + ''.join(
        f'<option value="{p}">{p}</option>' for p in projs
    )

    def stage_opt(s):
        label, tip = STAGE_META.get(s, (s, s))
        return f'<option value="{s}" title="{tip}" data-hint="{tip}">{label}</option>'
    stage_opts = ''.join(stage_opt(s) for s in stages)

    circ = round(2 * 3.14159265 * 26, 2)  # stroke-dasharray for r=26 → 163.36

    css = """
@import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;500;600;700&family=DM+Sans:ital,wght@0,300;0,400;0,500;0,600;1,400&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
  --bg:       #07090c;
  --surface:  #0d1117;
  --surface2: #111820;
  --border:   rgba(255,255,255,0.07);
  --borderhi: rgba(255,255,255,0.13);
  --text:     #dde1e6;
  --dim:      #8b939d;
  --faint:    #3e4650;
  --gold:     #c8a45c;
  --gold-bg:  rgba(200,164,92,0.10);
  --red:      #e05c4e;
  --green:    #4caf7d;
  --serif:    'Cormorant Garamond', Georgia, serif;
  --sans:     'DM Sans', system-ui, sans-serif;
  --mono:     'JetBrains Mono', 'Courier New', monospace;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }

body {
  font-family: var(--sans);
  background: var(--bg);
  color: var(--text);
  font-size: 15px;
  line-height: 1.65;
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}

.wrap {
  max-width: 1280px;
  margin: 0 auto;
  padding: 56px 64px;
}

/* ── HEADER ── */
.site-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  padding-bottom: 28px;
  margin-bottom: 36px;
  border-bottom: 1px solid var(--border);
  position: relative;
}
.site-header::after {
  content: '';
  position: absolute;
  bottom: -1px;
  left: 0;
  width: 44px;
  height: 1px;
  background: var(--gold);
}
.logo {
  font-family: var(--serif);
  font-size: 38px;
  font-weight: 600;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: var(--text);
  line-height: 1;
  margin-bottom: 9px;
}
.logo-sub {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--faint);
}
.v-badge {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--gold);
  border: 1px solid rgba(200,164,92,0.35);
  padding: 4px 10px;
}
.header-side {
  display: flex;
  align-items: flex-end;
  gap: 14px;
}
.owner-badge {
  font-size: 10px;
  color: var(--gold);
  border: 1px solid rgba(200,164,92,0.4);
  padding: 3px 8px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  white-space: nowrap;
}
.balance-widget {
  min-width: 240px;
  padding: 10px 14px;
  border: 1px solid var(--border);
  background: var(--surface);
}
.balance-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.balance-main {
  color: var(--text);
  font-size: 14px;
  line-height: 1.4;
}
.balance-main.low { color: #ff9b52; }
.balance-main.danger { color: var(--red); }
.balance-sub {
  margin-top: 3px;
  color: var(--dim);
  font-size: 11px;
}
.balance-refresh {
  background: none;
  border: 1px solid rgba(200,164,92,0.35);
  color: var(--gold);
  cursor: pointer;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1;
  padding: 4px 7px;
}
.balance-refresh:hover { background: var(--gold-bg); }

/* ── TOOLBAR ── */
.toolbar {
  display: flex;
  margin-bottom: 32px;
  border-bottom: 1px solid var(--border);
}
.t-btn {
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--dim);
  cursor: pointer;
  font-family: var(--sans);
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  padding: 12px 22px 14px;
  margin-bottom: -1px;
  transition: color 0.18s, border-color 0.18s;
}
.t-btn:hover { color: var(--text); border-bottom-color: var(--borderhi); }
.t-btn-action {
  background: none;
  border: 1px solid rgba(200,164,92,0.35);
  color: var(--gold);
  cursor: pointer;
  font-family: var(--mono);
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  padding: 5px 14px;
  margin: auto 0 auto 8px;
  transition: background 0.15s, border-color 0.15s;
}
.t-btn-action:hover { background: var(--gold-bg); border-color: rgba(200,164,92,0.7); }

/* ── GRID ── */
.g2 { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 24px; }

/* ── CARD ── */
.card { background: var(--surface); border: 1px solid var(--border); margin-bottom: 24px; }
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 28px;
  border-bottom: 1px solid var(--border);
}
.card-title {
  font-family: var(--serif);
  font-size: 20px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--text);
}
.card-body { padding: 28px; }
.card-body.collapsed { display: none; }
.card-toggle {
  background: none;
  border: 1px solid rgba(200,164,92,0.35);
  color: var(--gold);
  cursor: pointer;
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  padding: 5px 12px;
  transition: background 0.15s, border-color 0.15s;
}
.card-toggle:hover { background: var(--gold-bg); border-color: rgba(200,164,92,0.7); }

/* ── FIELDS ── */
.field { margin-bottom: 18px; }
.field:last-of-type { margin-bottom: 0; }
.f-label {
  display: block;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--faint);
  margin-bottom: 7px;
}
select, input[type="text"], textarea {
  width: 100%;
  padding: 12px 15px;
  background: #060809;
  border: 1px solid var(--borderhi);
  color: var(--text);
  font-family: var(--sans);
  font-size: 15px;
  outline: none;
  appearance: none;
  -webkit-appearance: none;
  transition: border-color 0.15s;
}
select {
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5'%3E%3Cpath d='M0 0l4 5 4-5z' fill='%233e4650'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 15px center;
  padding-right: 36px;
  cursor: pointer;
}
select:focus, input:focus, textarea:focus { border-color: var(--gold); }
textarea { resize: vertical; min-height: 96px; line-height: 1.6; }
.f-hint {
  margin-top: 6px;
  font-size: 12px;
  color: var(--faint);
  font-style: italic;
  min-height: 16px;
}

/* ── BUTTONS ── */
.btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  margin-top: 8px;
  padding: 14px 24px;
  font-family: var(--sans);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.13em;
  text-transform: uppercase;
  cursor: pointer;
  border: none;
  transition: opacity 0.15s;
}
.btn:disabled { opacity: 0.35; cursor: not-allowed; }
.btn-gold { background: var(--gold); color: #07090c; }
.btn-gold:hover:not(:disabled) { opacity: 0.84; }
.btn-ghost { background: transparent; border: 1px solid var(--borderhi); color: var(--dim); }
.btn-ghost:hover:not(:disabled) { border-color: var(--gold); color: var(--gold); }

/* ── PMF SCORE ── */
.pmf-row {
  display: flex;
  align-items: center;
  gap: 22px;
  margin-top: 20px;
  padding-top: 18px;
  border-top: 1px solid var(--border);
}
.score-ring { position: relative; width: 64px; height: 64px; flex-shrink: 0; }
.score-ring svg { display: block; transform: rotate(-90deg); }
.score-num {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 500;
  color: var(--gold);
}
.score-lbl {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--faint);
  margin-bottom: 5px;
}
.score-meta { font-size: 12px; color: var(--dim); }

/* ── ACTIVITY ── */
.log-wrap {
  max-height: 210px;
  overflow-y: auto;
}
.log-wrap::-webkit-scrollbar { width: 3px; }
.log-wrap::-webkit-scrollbar-thumb { background: var(--borderhi); }
.log-row {
  display: grid;
  grid-template-columns: 50px 110px 1fr;
  gap: 14px;
  padding: 7px 0;
  border-bottom: 1px solid var(--border);
  font-family: var(--mono);
  font-size: 11px;
  align-items: baseline;
}
.log-row:last-child { border-bottom: none; }
.l-time  { color: var(--faint); }
.l-proj  { color: var(--gold); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.l-msg   { color: var(--dim);  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ── JOBS TABLE ── */
.j-table { width: 100%; border-collapse: collapse; }
.j-table th {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--faint);
  font-weight: 400;
  text-align: left;
  padding: 0 14px 10px 0;
  border-bottom: 1px solid var(--border);
}
.j-table td {
  padding: 9px 14px 9px 0;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.j-table tr:last-child td { border-bottom: none; }
.j-id { font-family: var(--mono); font-size: 11px; color: var(--faint); letter-spacing: 0.06em; }
.j-proj { font-size: 13px; color: var(--dim); }
.s-pill {
  display: inline-block;
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  padding: 3px 7px;
  border: 1px solid;
}
.s-done    { color: var(--green); border-color: rgba(76,175,125,0.35); }
.s-run     { color: var(--gold);  border-color: rgba(200,164,92,0.35); }
.s-queue   { color: var(--faint); border-color: var(--borderhi); }
.s-fail    { color: var(--red);   border-color: rgba(224,92,78,0.35); }
.prog { width: 80px; height: 2px; background: var(--borderhi); display: inline-block; vertical-align: middle; }
.prog-f { height: 100%; background: var(--gold); transition: width 0.4s; }
.dl { font-family: var(--mono); font-size: 10px; letter-spacing: 0.06em; color: var(--gold); text-decoration: none; }
.dl:hover { text-decoration: underline; }

/* ── GUESTS ── */
.guest-grid { display: grid; grid-template-columns: 1fr 1.4fr; gap: 24px; }
.guest-box + .guest-box { margin-top: 18px; }
.guest-subtitle {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--faint);
  margin-bottom: 10px;
}
.guest-empty { color: var(--dim); font-size: 13px; }
.guest-table { width: 100%; border-collapse: collapse; }
.guest-table th, .guest-table td {
  text-align: left;
  padding: 9px 12px 9px 0;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
.guest-table th {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--faint);
  font-weight: 400;
}
.guest-table td {
  font-size: 13px;
  color: var(--dim);
}
.guest-table tr:last-child td { border-bottom: none; }
.guest-date {
  font-family: var(--serif);
  font-size: 23px;
  color: var(--text);
  margin: 0 0 14px;
}
.guest-user {
  padding: 13px 0;
  border-top: 1px solid var(--border);
}
.guest-user:first-of-type { border-top: none; padding-top: 0; }
.guest-user-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.guest-user-name { color: var(--text); font-size: 14px; font-weight: 600; }
.guest-user-project { color: var(--gold); font-size: 12px; }
.guest-event {
  display: grid;
  grid-template-columns: 62px 90px 1fr;
  gap: 12px;
  padding: 4px 0;
  font-size: 12px;
}
.guest-event-time, .guest-event-type { font-family: var(--mono); color: var(--faint); }
.guest-event-detail { color: var(--dim); }
.guest-status-ok { color: var(--green); }
.guest-status-none { color: var(--dim); }
.guest-action-btn {
  background: none;
  border: 1px solid var(--borderhi);
  color: var(--text);
  cursor: pointer;
  font-family: var(--sans);
  font-size: 12px;
  padding: 6px 10px;
  transition: border-color 0.15s, color 0.15s, background 0.15s;
}
.guest-action-btn:hover {
  border-color: rgba(200,164,92,0.5);
  color: var(--gold);
  background: rgba(200,164,92,0.06);
}

@media (max-width: 900px) {
  .site-header { align-items: flex-start; gap: 16px; flex-direction: column; }
  .header-side { width: 100%; align-items: stretch; flex-direction: column; }
  .balance-widget { min-width: 0; width: 100%; }
  .guest-grid { grid-template-columns: 1fr; }
}

/* ── CHAT ── */
.chat-overlay {
  position: fixed;
  bottom: 28px; right: 28px;
  width: 350px;
  background: var(--surface);
  border: 1px solid var(--borderhi);
  display: none;
  flex-direction: column;
  z-index: 100;
  box-shadow: 0 28px 72px rgba(0,0,0,0.72);
}
.chat-hd {
  padding: 13px 18px;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.chat-lbl { font-family: var(--mono); font-size: 9px; letter-spacing: 0.2em; text-transform: uppercase; color: var(--dim); }
.chat-x { background: none; border: none; color: var(--faint); cursor: pointer; font-size: 18px; line-height: 1; padding: 0; transition: color 0.15s; }
.chat-x:hover { color: var(--text); }
.chat-msgs {
  height: 260px;
  overflow-y: auto;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 9px;
}
.chat-msgs::-webkit-scrollbar { width: 3px; }
.chat-msgs::-webkit-scrollbar-thumb { background: var(--borderhi); }
.msg { font-size: 13px; line-height: 1.5; padding: 10px 13px; max-width: 88%; }
.msg-user { background: var(--gold-bg); border-left: 2px solid var(--gold); align-self: flex-end; color: var(--text); }
.msg-ai   { background: var(--surface2); border-left: 2px solid var(--borderhi); color: var(--dim); }
.chat-ft { border-top: 1px solid var(--border); display: flex; }
.chat-ft input {
  flex: 1;
  background: transparent;
  border: none;
  color: var(--text);
  padding: 11px 15px;
  font-family: var(--sans);
  font-size: 13px;
  outline: none;
}
.chat-ft button {
  background: var(--gold);
  border: none;
  color: #07090c;
  padding: 0 18px;
  cursor: pointer;
  font-weight: 700;
  font-size: 15px;
  transition: opacity 0.15s;
}
.chat-ft button:hover { opacity: 0.84; }
.chat-feedback {
  padding: 8px 14px 10px;
  border-top: 1px solid var(--border);
}
.chat-fb-toggle {
  background: none;
  border: none;
  color: var(--dim);
  font-size: 11px;
  cursor: pointer;
  padding: 0;
  text-decoration: underline;
}
.chat-fb-toggle:hover { color: var(--text); }

/* ── RAG ── */
.chat-filter {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 8px 14px;
  border-bottom: 1px solid var(--border);
}
.chat-filter select {
  flex: 1;
  padding: 6px 28px 6px 10px;
  font-size: 12px;
  background: #060809;
  width: auto;
}
.rag-toggle {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  padding: 5px 10px;
  background: transparent;
  border: 1px solid var(--borderhi);
  color: var(--faint);
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
  white-space: nowrap;
  flex-shrink: 0;
}
.rag-toggle.active { border-color: rgba(200,164,92,0.5); color: var(--gold); }
.chat-sources {
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  background: var(--surface2);
  display: none;
  max-height: 120px;
  overflow-y: auto;
}
.chat-sources::-webkit-scrollbar { width: 3px; }
.chat-sources::-webkit-scrollbar-thumb { background: var(--borderhi); }
.src-header {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--faint);
  margin-bottom: 7px;
  display: block;
}
.src-item {
  padding: 5px 0;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
  line-height: 1.4;
}
.src-item:last-child { border-bottom: none; }
.src-type { color: var(--gold); font-family: var(--mono); font-size: 10px; }
.src-score { color: var(--faint); font-family: var(--mono); font-size: 10px; margin-left: 6px; }
.src-text { color: var(--dim); display: block; margin-top: 2px; }
"""

    js_auth = f"const TOKEN = '{OWNER_TOKEN}'; const headers = () => ({{ 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' }});"

    js_body = r"""
function showStageHint(sel) {
  const opt = sel.options[sel.selectedIndex];
  const el = document.getElementById('stageHint');
  if (el) el.textContent = opt ? (opt.getAttribute('data-hint') || '') : '';
}

function toggleCardBody(id, btn) {
  const body = document.getElementById(id);
  if (!body) return;
  const collapsed = body.classList.toggle('collapsed');
  if (btn) btn.textContent = collapsed ? 'Показать' : 'Скрыть';
}

function toggleChat() {
  const c = document.getElementById('aiChat');
  const open = window.getComputedStyle(c).display !== 'none';
  c.style.display = open ? 'none' : 'flex';
  if (!open) document.getElementById('chatInput').focus();
}

function appendMsg(text, type) {
  const c = document.getElementById('chatMessages');
  const d = document.createElement('div');
  d.className = 'msg msg-' + type;
  d.textContent = text;
  c.appendChild(d);
  c.scrollTop = c.scrollHeight;
}

let useRag = true;

function toggleRagMode() {
  useRag = !useRag;
  const btn = document.getElementById('ragToggle');
  btn.textContent = 'RAG: ' + (useRag ? 'ON' : 'OFF');
  btn.classList.toggle('active', useRag);
}

async function reindexProject() {
  const proj = document.getElementById('projSelect1').value || document.getElementById('projSelect2').value;
  if (!proj) { alert('Сначала выбери проект'); return; }
  try {
    const fd = new URLSearchParams({ project: proj });
    const r = await fetch('/api/index', { method: 'POST', headers: headers(), body: fd });
    if (!r.ok) throw new Error(await r.text());
    loadTasks();
  } catch(e) { alert('Ошибка индексации: ' + e.message); }
}

async function sendChat() {
  const inp = document.getElementById('chatInput');
  const msg = inp.value.trim();
  if (!msg) return;
  appendMsg(msg, 'user');
  inp.value = '';
  document.getElementById('chatSources').style.display = 'none';
  appendMsg('…', 'ai');
  try {
    const proj = document.getElementById('projSelect1').value || document.getElementById('projSelect2').value;
    if (!proj) { document.getElementById('chatMessages').lastElementChild.remove(); appendMsg('Сначала выбери проект.', 'ai'); return; }
    const fd = new URLSearchParams({
      project: proj,
      query: msg,
      use_rag: useRag,
      filter_type: document.getElementById('ragFilter').value
    });
    const res = await fetch('/api/chat', { method: 'POST', headers: headers(), body: fd });
    const d = await res.json();
    document.getElementById('chatMessages').lastElementChild.remove();
    appendMsg(d.response, 'ai');
    if (d.mode === 'rag' && d.sources && d.sources.length) showSources(d.sources);
  } catch(e) {
    document.getElementById('chatMessages').lastElementChild.remove();
    appendMsg('Ошибка: ' + e.message, 'ai');
  }
}

function showSources(sources) {
  const panel = document.getElementById('chatSources');
  const list = document.getElementById('chatSourcesList');
  list.innerHTML = '';
  sources.forEach(s => {
    const item = document.createElement('div');
    item.className = 'src-item';
    const type = document.createElement('span');
    type.className = 'src-type';
    type.textContent = s.type || 'unknown';
    const score = document.createElement('span');
    score.className = 'src-score';
    score.textContent = 'score\u00a0' + s.score;
    const text = document.createElement('span');
    text.className = 'src-text';
    text.textContent = s.content + '\u2026';
    item.appendChild(type);
    item.appendChild(score);
    item.appendChild(text);
    list.appendChild(item);
  });
  panel.style.display = 'block';
}

let mediaRec = null, audioChunks = [];
async function toggleRecord() {
  const btn = document.getElementById('btnVoice');
  if (mediaRec && mediaRec.state === 'recording') { mediaRec.stop(); return; }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    alert('🎤 Запись микрофона доступна только по HTTPS.\nИспользуй голосовые сообщения напрямую в Telegram-боте.');
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioChunks = [];
    mediaRec = new MediaRecorder(stream);
    mediaRec.ondataavailable = e => { if (e.data.size > 0) audioChunks.push(e.data); };
    mediaRec.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      btn.textContent = 'Voice Note';
      const proj = document.getElementById('projSelect1').value || document.getElementById('projSelect2').value;
      if (!proj) { alert('Select a project first'); return; }
      const blob = new Blob(audioChunks, { type: 'audio/webm' });
      const reader = new FileReader();
      reader.onload = async () => {
        try {
          const fd = new URLSearchParams({ project: proj, audio_base64: reader.result });
          const r = await fetch('/api/voice', { method: 'POST', headers: headers(), body: fd });
          const d = await r.json();
          alert('Recording sent. Transcription in progress.\nResult: ' + d.transcript);
        } catch(e) { alert('Error: ' + e.message); }
      };
      reader.readAsDataURL(blob);
    };
    mediaRec.start();
    btn.textContent = 'Stop';
  } catch(e) { alert('Microphone access denied: ' + e.message); }
}

async function loadScore() {
  const p = document.getElementById('projSelect1').value || document.getElementById('projSelect2').value;
  if (!p) { alert('Select a project first'); return; }
  const r = await fetch('/api/pmf_score?project=' + encodeURIComponent(p), { headers: headers() });
  const d = await r.json();
  const pct = d.score;
  const circumference = 2 * Math.PI * 26;
  const offset = circumference - (pct / 100) * circumference;
  const arc = document.getElementById('scoreArc');
  if (arc) arc.style.strokeDashoffset = offset;
  const num = document.getElementById('scoreNum');
  if (num) num.textContent = pct + '%';
  const meta = document.getElementById('scoreMeta');
  if (meta) {
    const freshness = d.context_days !== null ? ' \u00b7 контекст ' + d.context_days + ' дн. назад' : '';
    const done = d.completed && d.completed.length > 0 ? d.completed.length + '/' + d.total + ' этапов' : '0/' + d.total + ' этапов';
    meta.textContent = done + freshness;
    meta.title = d.completed && d.completed.length > 0 ? 'Готово: ' + d.completed.join(', ') : 'Нет завершённых этапов';
  }
}

async function loadActivity() {
  try {
    const r = await fetch('/api/activity', { headers: headers() });
    const d = await r.json();
    const feed = document.getElementById('activityFeed');
    if (!d.length) return;
    feed.innerHTML = d.slice(-30).reverse().map(x =>
      '<div class="log-row">' +
      '<span class="l-time">' + x.time + '</span>' +
      '<span class="l-proj">' + (x.project || '\u2014') + '</span>' +
      '<span class="l-msg">' + x.action + (x.details ? ' \u00b7 ' + x.details : '') + '</span>' +
      '</div>'
    ).join('');
  } catch(e) {}
}

async function loadBalance() {
  const mainEl = document.getElementById('balanceMain');
  const subEl = document.getElementById('balanceSub');
  if (!mainEl || !subEl) return;
  mainEl.textContent = '💰 OpenRouter: ...';
  subEl.textContent = 'Загрузка баланса';
  mainEl.classList.remove('low', 'danger');
  try {
    const r = await fetch('/api/balance', { headers: headers() });
    const data = await r.json();
    if (!data.ok) {
      mainEl.textContent = '💰 OpenRouter: недоступно';
      subEl.textContent = 'Не удалось получить баланс';
      return;
    }
    const remaining = typeof data.remaining === 'number' ? data.remaining : null;
    const usage = typeof data.usage === 'number' ? data.usage : 0;
    const limit = typeof data.limit === 'number' ? data.limit : null;
    const fmt = v => '$' + v.toFixed(4);
    mainEl.textContent = 'OpenRouter: ' + (remaining === null ? 'лимит не задан' : (fmt(remaining) + ' остаток'));
    if (remaining !== null && remaining < 1) mainEl.classList.add('danger');
    else if (remaining !== null && remaining < 2) mainEl.classList.add('low');
    subEl.textContent = limit === null
      ? ('Использовано ' + fmt(usage))
      : ('Использовано ' + fmt(usage) + ' из ' + fmt(limit));
  } catch(e) {
    mainEl.textContent = '💰 OpenRouter: ошибка';
    subEl.textContent = 'Не удалось загрузить баланс';
  }
}

async function loadTasks() {
  try {
    const r = await fetch('/api/jobs', { headers: headers() });
    const j = await r.json();
    const cls = { completed: 's-done', running: 's-run', queued: 's-queue', failed: 's-fail' };
    document.getElementById('tBody').innerHTML = j.slice(-20).reverse().map(x =>
      '<tr>' +
      '<td class="j-id">' + x.id.slice(0, 8) + '</td>' +
      '<td class="j-proj">' + x.project + '</td>' +
      '<td><span class="s-pill ' + (cls[x.status] || 's-queue') + '">' + x.status + '</span></td>' +
      '<td><div class="prog"><div class="prog-f" style="width:' + x.progress + '%"></div></div></td>' +
      '<td>' + (x.file ? '<a href="' + x.file + '" class="dl">Download</a>' : '') + '</td>' +
      '</tr>'
    ).join('');
  } catch(e) {}
}

function escapeHtml(text) {
  return String(text || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatGuestDate(ts) {
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts || '';
  return new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }).format(date);
}

function formatGuestTime(ts) {
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

async function loadGuests() {
  try {
    const r = await fetch('/api/guests', { headers: headers() });
    const data = await r.json();
    const pending = Object.entries(data.pending_tokens || {});
    const tokensEl = document.getElementById('guestTokens');
    const membersEl = document.getElementById('groupMembers');
    const timelineEl = document.getElementById('guestTimeline');

    if (!pending.length) {
      tokensEl.innerHTML = '<div class="guest-empty">Нет активных приглашений</div>';
    } else {
      tokensEl.innerHTML =
        '<table class="guest-table"><thead><tr><th>Токен</th><th>Проект</th><th>Истекает</th></tr></thead><tbody>' +
        pending.map(([token, info]) =>
          '<tr>' +
          '<td>' + escapeHtml(token) + '</td>' +
          '<td>' + escapeHtml(info.project || '') + '</td>' +
          '<td>' + escapeHtml(info.expires_at || '') + '</td>' +
          '</tr>'
        ).join('') +
        '</tbody></table>';
    }

    const rows = [];
    Object.entries(data.group_members || {}).forEach(([project, users]) => {
      Object.entries(users || {}).forEach(([userId, username]) => {
        const hasAccess = !!(((data.guests_by_project || {})[project] || {})[userId]);
        rows.push({
          project,
          user_id: String(userId),
          username: username || String(userId),
          hasAccess,
        });
      });
    });

    if (!rows.length) {
      membersEl.innerHTML = '<div class="guest-empty">Участники групп не найдены</div>';
    } else {
      membersEl.innerHTML =
        '<table class="guest-table"><thead><tr><th>Username</th><th>Проект</th><th>Статус</th><th>Действие</th></tr></thead><tbody>' +
        rows.sort((a, b) => (a.project + a.username).localeCompare(b.project + b.username, 'ru')).map(row =>
          '<tr>' +
          '<td>' + escapeHtml(row.username) + '</td>' +
          '<td>' + escapeHtml(row.project) + '</td>' +
          '<td class="' + (row.hasAccess ? 'guest-status-ok' : 'guest-status-none') + '">' + (row.hasAccess ? 'Гость ✅' : 'Нет доступа ➖') + '</td>' +
          '<td><button class="guest-action-btn" onclick="' + (row.hasAccess
            ? ("revokeGuest('" + encodeURIComponent(row.user_id) + "')")
            : ("inviteGuest('" + encodeURIComponent(row.project) + "')")) + '">' + (row.hasAccess ? '❌ Отозвать' : '🔗 Пригласить') + '</button></td>' +
          '</tr>'
        ).join('') +
        '</tbody></table>';
    }

    const grouped = {};
    (data.activity || []).forEach(item => {
      const dateKey = item.ts ? item.ts.slice(0, 10) : 'unknown';
      if (!grouped[dateKey]) grouped[dateKey] = {};
      const userKey = String(item.user_id || 'unknown');
      if (!grouped[dateKey][userKey]) {
        grouped[dateKey][userKey] = {
          username: item.username || userKey,
          project: item.project || '—',
          items: [],
        };
      }
      grouped[dateKey][userKey].items.push(item);
    });

    const dates = Object.keys(grouped).sort().reverse();
    if (!dates.length) {
      timelineEl.innerHTML = '<div class="guest-empty">Активности гостей пока нет</div>';
      return;
    }

    timelineEl.innerHTML = dates.map(dateKey => {
      const users = Object.values(grouped[dateKey]);
      return (
        '<div class="guest-box">' +
        '<div class="guest-date">📅 ' + escapeHtml(formatGuestDate(dateKey)) + '</div>' +
        users.map(user => (
          '<div class="guest-user">' +
          '<div class="guest-user-head">' +
          '<span class="guest-user-name">👤 ' + escapeHtml(user.username) + '</span>' +
          '<span class="guest-user-project">(' + escapeHtml(user.project) + ')</span>' +
          '</div>' +
          user.items.sort((a, b) => String(a.ts).localeCompare(String(b.ts))).map(item =>
            '<div class="guest-event">' +
            '<span class="guest-event-time">' + escapeHtml(formatGuestTime(item.ts)) + '</span>' +
            '<span class="guest-event-type">' + escapeHtml(item.event || '') + ':</span>' +
            '<span class="guest-event-detail">' + escapeHtml(item.detail || '') + '</span>' +
            '</div>'
          ).join('') +
          '</div>'
        )).join('') +
        '</div>'
      );
    }).join('');
  } catch(e) {}
}

async function inviteGuest(project) {
  try {
    const r = await fetch('/api/invite', {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ project: decodeURIComponent(project) })
    });
    const data = await r.json();
    if (!data.ok) {
      alert(data.error || 'Не удалось создать приглашение');
      return;
    }
    const link = data.link || (data.bot_username ? ('https://t.me/' + data.bot_username + '?start=' + data.token) : ('t.me/<bot_username>?start=' + data.token));
    window.prompt('Скопируй ссылку приглашения:', link);
    loadGuests();
  } catch(e) {
    alert('Ошибка приглашения: ' + e.message);
  }
}

async function revokeGuest(userId) {
  try {
    const r = await fetch('/api/revoke', {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ user_id: decodeURIComponent(userId) })
    });
    const data = await r.json();
    if (!data.ok) {
      alert(data.error || 'Не удалось отозвать доступ');
      return;
    }
    loadGuests();
  } catch(e) {
    alert('Ошибка отзыва: ' + e.message);
  }
}

function toggleChatFeedback() {
  const p = document.getElementById('chatFeedbackPanel');
  p.style.display = p.style.display === 'none' ? 'block' : 'none';
}

async function submitChatFeedback() {
  const text = document.getElementById('chatFeedbackText').value.trim();
  const statusEl = document.getElementById('chatFeedbackStatus');
  if (!text) return;
  statusEl.textContent = '...';
  try {
    const r = await fetch('/api/feedback', { method: 'POST', headers: { ...headers(), 'Content-Type': 'application/json' }, body: JSON.stringify({text}) });
    const d = await r.json();
    statusEl.textContent = d.ok ? 'Записано' : 'Ошибка';
    if (d.ok) { document.getElementById('chatFeedbackText').value = ''; }
  } catch(e) { statusEl.textContent = 'Ошибка'; }
}

async function submitForm(formId, btnId, overrideUrl) {
  const f = document.getElementById(formId);
  const b = document.getElementById(btnId);
  b.disabled = true;
  try {
    const fd = new FormData(f);
    const url = overrideUrl || ('/api/' + (formId === 'runForm' ? 'run' : 'ctx'));
    const r = await fetch(url, {
      method: 'POST',
      headers: { ...headers(), 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams(fd)
    });
    if (!r.ok) throw new Error(await r.text());
    f.reset();
    loadTasks();
  } catch(e) { alert(e.message); }
  finally { b.disabled = false; }
}

function rememberProject(val) {
  if (val) localStorage.setItem('pmf_last_project', val);
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('runForm').onsubmit = e => { e.preventDefault(); submitForm('runForm', 'btnRun'); };
  document.getElementById('ctxForm').onsubmit = e => { e.preventDefault(); submitForm('ctxForm', 'btnCtx', '/api/ctx_and_index'); };
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'b') { e.preventDefault(); toggleChat(); }
  });

  const saved = localStorage.getItem('pmf_last_project');
  if (saved) {
    ['projSelect1', 'projSelect2'].forEach(id => {
      const sel = document.getElementById(id);
      if (sel && [...sel.options].some(o => o.value === saved)) sel.value = saved;
    });
    loadScore();
  }

  document.getElementById('projSelect1').addEventListener('change', e => rememberProject(e.target.value));
  document.getElementById('projSelect2').addEventListener('change', e => rememberProject(e.target.value));

  loadTasks();
  loadActivity();
  loadBalance();
  loadGuests();
  setInterval(loadTasks, 2000);
  setInterval(loadActivity, 5000);
  setInterval(loadBalance, 60000);
  setInterval(loadScore, 10000);
});
"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PMF Pipeline</title>
<style>{css}</style>
</head>
<body>
<div class="wrap">

  <header class="site-header">
    <div>
      <div class="logo">PMF Pipeline</div>
      <div class="logo-sub">Анализ продуктово-рыночного соответствия</div>
    </div>
    <div class="header-side">
      <div class="balance-widget">
        <div class="balance-top">
          <div class="balance-main" id="balanceMain">OpenRouter: ...</div>
          <button class="balance-refresh" onclick="loadBalance()" title="Обновить баланс">↻</button>
        </div>
        <div class="balance-sub" id="balanceSub">Загрузка баланса</div>
        <div class="balance-sub" style="margin-top:4px;"><a href="https://openrouter.ai/credits" target="_blank" style="color:var(--gold);text-decoration:none;">Пополнить баланс →</a></div>
      </div>
      <div class="owner-badge">Личный ассистент</div>
      <span class="v-badge">v4.0</span>
    </div>
  </header>

  <div class="toolbar">
    <button class="t-btn" onclick="toggleChat()">AI-чат</button>
    <button class="t-btn" id="btnVoice" onclick="toggleRecord()">Голос</button>
    <button class="t-btn" onclick="loadScore()">PMF Score</button>
    <button class="t-btn-action" onclick="reindexProject()" title="Только индексация без обработки контекста">Только индексировать</button>
  </div>

  <div class="g2">
    <div class="card">
      <div class="card-head"><div class="card-title">Запуск этапа</div></div>
      <div class="card-body">
        <form id="runForm">
          <div class="field">
            <label class="f-label">Проект</label>
            <select name="project" id="projSelect1" required onchange="loadScore()">{proj_opts}</select>
          </div>
          <div class="field">
            <label class="f-label">Этап</label>
            <select name="stage" id="stageSelect" required onchange="showStageHint(this)">{stage_opts}</select>
            <div class="f-hint" id="stageHint"></div>
          </div>
          <div class="field">
            <label class="f-label">Контекст / Задача</label>
            <textarea name="input" rows="4" placeholder="Опишите контекст или цель для данного этапа..." required></textarea>
          </div>
          <button type="submit" id="btnRun" class="btn btn-gold">Запустить</button>
        </form>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><div class="card-title">Контекст</div></div>
      <div class="card-body">
        <form id="ctxForm">
          <div class="field">
            <label class="f-label">Проект</label>
            <select name="project" id="projSelect2" required>{proj_opts}</select>
          </div>
          <div class="field">
            <label class="f-label">Источник</label>
            <select name="mode" required>
              <option value="all">Все источники</option>
              <option value="group">Группа</option>
              <option value="inbox">Заметки</option>
            </select>
          </div>
          <button type="submit" id="btnCtx" class="btn btn-ghost">Обработать и индексировать</button>
        </form>
        <div class="pmf-row">
          <div class="score-ring">
            <svg width="64" height="64" viewBox="0 0 64 64">
              <circle cx="32" cy="32" r="26" fill="none" stroke="rgba(255,255,255,0.05)" stroke-width="3"/>
              <circle id="scoreArc" cx="32" cy="32" r="26" fill="none" stroke="#c8a45c" stroke-width="3"
                stroke-linecap="butt"
                stroke-dasharray="{circ}"
                stroke-dashoffset="{circ}"/>
            </svg>
            <div class="score-num" id="scoreNum">&mdash;</div>
          </div>
          <div>
            <div class="score-lbl">PMF Readiness</div>
            <div class="score-meta" id="scoreMeta">Нажми PMF Score для расчёта</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-head">
      <div class="card-title">Гости и активность</div>
      <div>
        <button class="card-toggle" onclick="loadGuests()">Обновить</button>
        <button class="card-toggle" onclick="toggleCardBody('guestsBody', this)">Скрыть</button>
      </div>
    </div>
    <div class="card-body" id="guestsBody">
      <div class="guest-grid">
        <div>
          <div class="guest-subtitle">Активные инвайты</div>
          <div id="guestTokens" class="guest-box">
            <div class="guest-empty">Загрузка...</div>
          </div>
          <div class="guest-subtitle" style="margin-top:18px;">Участники групп</div>
          <div id="groupMembers" class="guest-box">
            <div class="guest-empty">Загрузка...</div>
          </div>
        </div>
        <div>
          <div class="guest-subtitle">Таймлайн гостей</div>
          <div id="guestTimeline" class="guest-box">
            <div class="guest-empty">Загрузка...</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-head"><div class="card-title">Лог активности</div></div>
    <div class="card-body" style="padding-top:18px;padding-bottom:18px;">
      <div class="log-wrap" id="activityFeed">
        <div class="log-row">
          <span class="l-time">&mdash;</span>
          <span class="l-proj">&mdash;</span>
          <span class="l-msg">Активности пока нет</span>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-head"><div class="card-title">Задачи</div></div>
    <div class="card-body">
      <table class="j-table">
        <thead>
          <tr>
            <th>ID</th><th>Проект</th><th>Статус</th><th>Прогресс</th><th>Файл</th>
          </tr>
        </thead>
        <tbody id="tBody"></tbody>
      </table>
    </div>
  </div>

</div>

<div class="chat-overlay" id="aiChat">
  <div class="chat-hd">
    <span class="chat-lbl">AI-ассистент</span>
    <button class="chat-x" onclick="toggleChat()">&times;</button>
  </div>
  <div class="chat-filter">
    <select id="ragFilter">
      <option value="">Все типы</option>
      <option value="context_digest">Контекст</option>
      <option value="inbox_note">Заметки</option>
    </select>
    <button class="rag-toggle active" id="ragToggle" onclick="toggleRagMode()">RAG: ON</button>
  </div>
  <div class="chat-msgs" id="chatMessages"></div>
  <div class="chat-sources" id="chatSources">
    <span class="src-header">Источники</span>
    <div id="chatSourcesList"></div>
  </div>
  <form class="chat-ft" onsubmit="event.preventDefault(); sendChat()">
    <input type="text" id="chatInput" placeholder="Спроси про гипотезы, метрики, следующие шаги...">
    <button type="submit">&rarr;</button>
  </form>
  <div class="chat-feedback">
    <button class="chat-fb-toggle" onclick="toggleChatFeedback()">Оставить замечание</button>
    <div id="chatFeedbackPanel" style="display:none;margin-top:8px;">
      <textarea id="chatFeedbackText" rows="2" placeholder="Опиши что не так в работе бота..." style="width:100%;box-sizing:border-box;resize:none;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:6px 8px;font-size:12px;"></textarea>
      <div style="display:flex;gap:8px;margin-top:6px;align-items:center;">
        <button onclick="submitChatFeedback()" style="font-size:12px;padding:4px 10px;">Отправить</button>
        <span id="chatFeedbackStatus" style="font-size:11px;color:var(--dim);"></span>
      </div>
    </div>
  </div>
</div>

<script>
{js_auth}
{js_body}
</script>
</body>
</html>"""
    return HTMLResponse(content=html)

# === 7. V4.0 ADDON ENDPOINTS ===
@app.post("/api/chat", dependencies=[Depends(get_access)])
async def api_chat(
    project: str = Form(...),
    query: str = Form(...),
    use_rag: str = Form("true"),
    filter_type: str = Form("")
):
    p = PROJECTS_ROOT / project
    if not p.exists(): raise HTTPException(404, "Project not found")
    log_activity(project, "chat", query[:50])

    if RAG_AVAILABLE and use_rag.lower() == "true":
        try:
            results = rag_engine.search(project, query, top_k=5, filter_type=filter_type or None)
            if not results:
                return {"response": "Индекс проекта пуст. Нажмите «Индексировать» в панели инструментов, затем повторите вопрос.", "sources": [], "mode": "rag"}
            context_parts = [f"[{r['metadata'].get('type', 'unknown')}] {r['content']}" for r in results]
            context = "\n\n---\n\n".join(context_parts)
            if not context.strip():
                return {"response": "По вашему запросу ничего не найдено. Попробуйте сформулировать иначе или запустите индексацию проекта.", "sources": [], "mode": "rag"}
            prompt = f"""Ты — продуктовый ассистент проекта {project}.

Контекст из базы знаний (релевантные фрагменты):
{context}

Вопрос пользователя: {query}

Правила:
1. Отвечай ТОЛЬКО на основе предоставленного контекста.
2. Если в контексте нет ответа — скажи "В базе знаний нет информации по этому вопросу."
3. Максимум 300 слов. Структурированный ответ."""
            response = await run_with_retry(_call_llm, args=(cfg["llm"]["openrouter"]["draft_model"], prompt), max_retries=2)
            return {
                "response": response,
                "sources": [{"content": r["content"][:200], "type": r["metadata"].get("type"), "score": round(r["score"], 2)} for r in results],
                "mode": "rag"
            }
        except Exception as e:
            logger.error("RAG error: %s, falling back to full context", e)

    return {"response": await chat_with_project(p, query), "mode": "full_context"}

@app.post("/api/index", dependencies=[Depends(require_owner)])
async def api_index_project(bg: BackgroundTasks, project: str = Form(...)):
    p = PROJECTS_ROOT / project
    if not p.exists(): raise HTTPException(404, "Project not found")
    if not RAG_AVAILABLE: raise HTTPException(503, "RAG engine not available: install chromadb and sentence-transformers")
    jid = str(uuid.uuid4())
    _task_set(jid, {"id": jid, "type": "indexing", "status": "queued", "progress": 0, "project": project, "file": None, "status_msg": "In queue"})

    async def do_index():
        _task_update(jid, status="running", progress=10, status_msg="Indexing documents...")
        try:
            result = await asyncio.to_thread(rag_engine.index_project, project)
            _task_update(jid, status="completed", progress=100, status_msg=f"Indexed {result['indexed_docs']} docs")
            log_activity(project, "indexed", f"{result['indexed_docs']} docs")
        except Exception as e:
            _task_update(jid, status="failed", progress=0, status_msg=f"Error: {e}", error=str(e))
        finally:
            cleanup_tasks()

    bg.add_task(do_index)
    return {"job_id": jid, "status": "queued"}

@app.get("/api/index/{project}", dependencies=[Depends(get_access)])
async def api_index_stats(project: str):
    if not RAG_AVAILABLE:
        return {"project": project, "documents": 0, "status": "unavailable"}
    return rag_engine.get_stats(project)

@app.get("/api/pmf_score", dependencies=[Depends(get_access)])
async def api_pmf_score(project: str):
    p = PROJECTS_ROOT / project
    if not p.exists(): raise HTTPException(404)

    # Score based on which methodology artifacts actually exist in output/
    artifacts = [
        ("00_setup.md",             "Настройка проекта"),
        ("narrative-v1.md",         "Гипотеза (7 измерений)"),
        ("market-research.md",      "Исследование рынка"),
        ("risk-prioritization.md",  "Приоритизация рисков"),
        ("assumptions-map.md",      "Валидация DVF"),
        ("interview-guide.md",      "Подготовка к интервью"),
        ("interview-synthesis.md",  "Синтез интервью"),
        ("metrics-dashboard.md",    "Метрики Sean Ellis"),
        ("iteration-changelog.md",  "Итерация"),
    ]
    output_dir = p / "output"
    completed = [name for fname, name in artifacts if (output_dir / fname).exists()]
    score = round(len(completed) / len(artifacts) * 100)

    # Freshness bonus: +5 if context_digest updated in last 3 days
    digest = p / "docs" / "context_digest.md"
    if digest.exists():
        age_days = int((datetime.now().timestamp() - digest.stat().st_mtime) / 86400)
        if age_days < 3:
            score = min(100, score + 5)
    else:
        age_days = None

    return {
        "score": score,
        "completed": completed,
        "total": len(artifacts),
        "context_days": age_days,
    }

@app.get("/api/activity", dependencies=[Depends(get_access)])
async def api_activity():
    return json.loads(activity_path.read_text() or "[]")

@app.get("/api/balance", dependencies=[Depends(get_access)])
async def api_balance():
    from core.balance_monitor import check_balance
    result = await check_balance()
    if result is None:
        return {"ok": False}
    return {"ok": True, **result}

@app.get("/api/guests", dependencies=[Depends(require_owner)])
async def api_guests():
    projects_root = PROJECTS_ROOT

    guests_by_project = {}
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        guests_file = project_dir / "guests.json"
        if guests_file.exists():
            guests = json.loads(guests_file.read_text(encoding="utf-8"))
            if guests:
                guests_by_project[project_dir.name] = guests

    group_members = {}
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        users_file = project_dir / "users.json"
        if users_file.exists():
            import json as _json
            users = _json.loads(users_file.read_text(encoding="utf-8"))
            if users:
                group_members[project_dir.name] = users

    activity_file = BASE_DIR / "data" / "guest_activity.json"
    activity = json.loads(activity_file.read_text(encoding="utf-8")) if activity_file.exists() else []

    tokens_file = BASE_DIR / "data" / "invite_tokens.json"
    tokens = json.loads(tokens_file.read_text(encoding="utf-8")) if tokens_file.exists() else {}
    pending_tokens = {k: v for k, v in tokens.items() if not v.get("used")}

    return {
        "guests_by_project": guests_by_project,
        "activity": activity,
        "pending_tokens": pending_tokens,
        "group_members": group_members,
    }

@app.post("/api/invite", dependencies=[Depends(require_owner)])
async def api_invite(request: Request):
    body = await request.json()
    project = (body.get("project") or "").strip()
    if not project:
        return {"ok": False, "error": "no project"}
    project_path = PROJECTS_ROOT / project
    if not project_path.exists():
        return {"ok": False, "error": "project not found"}
    import secrets
    from datetime import datetime, timedelta
    token = "inv_" + secrets.token_urlsafe(8)
    tokens_file = BASE_DIR / "data" / "invite_tokens.json"
    tokens_file.parent.mkdir(exist_ok=True)
    tokens = json.loads(tokens_file.read_text(encoding="utf-8")) if tokens_file.exists() else {}
    tokens[token] = {
        "project": project,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "expires_at": (datetime.now() + timedelta(hours=48)).isoformat(timespec="seconds"),
        "used": False,
        "used_by": None,
    }
    tokens_file.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")
    bot_username = (
        cfg.get("bot_username")
        or cfg.get("telegram_bot_username")
        or cfg.get("bot", {}).get("username")
        or cfg.get("telegram", {}).get("bot_username")
    )
    response = {"ok": True, "token": token}
    if bot_username:
        response["bot_username"] = bot_username
        response["link"] = f"https://t.me/{bot_username}?start={token}"
    return response

@app.post("/api/revoke", dependencies=[Depends(require_owner)])
async def api_revoke(request: Request):
    body = await request.json()
    user_id = str(body.get("user_id") or "").strip()
    if not user_id:
        return {"ok": False, "error": "no user_id"}
    projects_root = PROJECTS_ROOT
    removed_from = []
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        guests_file = project_dir / "guests.json"
        if guests_file.exists():
            guests = json.loads(guests_file.read_text(encoding="utf-8"))
            if user_id in guests:
                del guests[user_id]
                guests_file.write_text(json.dumps(guests, ensure_ascii=False, indent=2), encoding="utf-8")
                removed_from.append(project_dir.name)
    return {"ok": True, "removed_from": removed_from}

@app.post("/api/feedback", dependencies=[Depends(get_access)])
async def api_feedback(request: Request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty"}
    from datetime import datetime
    from pathlib import Path
    feedback_file = Path(__file__).resolve().parent.parent / "data" / "bot_feedback.md"
    feedback_file.parent.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = (
        f"## [{ts}] | 🌐 webui\n"
        f"**Контекст:** Web UI\n"
        f"**Замечание:** {text}\n"
        f"**Решение:** —\n\n"
        f"---\n\n"
    )
    existing = feedback_file.read_text(encoding="utf-8") if feedback_file.exists() else ""
    feedback_file.write_text(entry + existing, encoding="utf-8")
    return {"ok": True}

async def _transcribe_and_save(audio_bytes: bytes, inbox: Path, stem: str):
    try:
        from core.transcriber import transcribe_audio
        text = await transcribe_audio(audio_bytes, suffix=".webm")
        if text:
            atomic_write(inbox / f"{stem}.md", f"# Голосовая заметка\n\n{text}\n")
            logger.info("Transcribed %s: %d chars", stem, len(text))
    except Exception as e:
        logger.error("Transcription failed for %s: %s", stem, e)

@app.post("/api/voice", dependencies=[Depends(get_access)])
async def api_voice(bg: BackgroundTasks, project: str = Form(...), audio_base64: str = Form(...)):
    p = PROJECTS_ROOT / project
    if not p.exists(): raise HTTPException(404)
    inbox = p / "inbox"
    inbox.mkdir(exist_ok=True)
    stem = f"voice_{uuid.uuid4().hex[:6]}"
    audio_bytes = base64.b64decode(audio_base64.split(",")[-1])
    (inbox / f"{stem}.webm").write_bytes(audio_bytes)
    bg.add_task(_transcribe_and_save, audio_bytes, inbox, stem)
    log_activity(project, "voice", f"Transcribing {stem}")
    return {"status": "transcribing", "file": f"{stem}.webm", "transcript": f"{stem}.md"}

@app.get("/api/tags", dependencies=[Depends(get_access)])
async def api_tags(project: str):
    p = PROJECTS_ROOT / project / "tags.json"
    return json.loads(p.read_text()) if p.exists() else []

@app.post("/api/schedule", dependencies=[Depends(require_owner)])
async def api_schedule(cron: str = Form(...), enabled: bool = Form(True)):
    cfg.setdefault("webui", {}).setdefault("features", {})["auto_schedule"] = {"enabled": enabled, "cron": cron}
    atomic_write(BASE_DIR / "config.yaml", yaml.dump(cfg, sort_keys=False, allow_unicode=True))
    return {"status": "saved"}

# === 8. V3.0+V4.0 CORE ROUTES ===
@app.post("/api/run", dependencies=[Depends(require_owner)])
async def queue_task(bg: BackgroundTasks, project: str = Form(...), stage: str = Form(...), input: str = Form(...)):
    p = PROJECTS_ROOT / project
    if not p.exists(): raise HTTPException(404)
    jid = str(uuid.uuid4())
    _task_set(jid, {"id": jid, "status": "queued", "progress": 0, "stage": stage, "project": project, "file": None})
    bg.add_task(execute_pipeline, jid, stage, input, p)
    log_activity(project, "stage_queued", stage)
    return {"job_id": jid}

@app.post("/api/ctx", dependencies=[Depends(get_access)])
async def queue_ctx(bg: BackgroundTasks, project: str = Form(...), mode: str = Form("all")):
    p = PROJECTS_ROOT / project
    if not p.exists(): raise HTTPException(404)
    jid = str(uuid.uuid4())
    _task_set(jid, {"id": jid, "status": "queued", "progress": 0, "project": project, "file": f"/download/{project}/docs/context_digest.md"})
    bg.add_task(execute_context_process, jid, project, mode)
    log_activity(project, "ctx_queued", mode)
    return {"job_id": jid}

@app.post("/api/ctx_and_index", dependencies=[Depends(require_owner)])
async def queue_ctx_and_index(bg: BackgroundTasks, project: str = Form(...), mode: str = Form("all")):
    p = PROJECTS_ROOT / project
    if not p.exists(): raise HTTPException(404)
    if not RAG_AVAILABLE: raise HTTPException(503, "RAG engine not available")
    jid = str(uuid.uuid4())
    _task_set(jid, {"id": jid, "status": "queued", "progress": 0, "project": project,
                  "file": f"/download/{project}/docs/context_digest.md", "status_msg": "In queue"})

    async def do_ctx_then_index():
        # Step 1: process context
        _task_update(jid, status="running", progress=10, status_msg="Обработка контекста...")
        try:
            await execute_context_process(jid, project, mode)
        except Exception as e:
            _task_update(jid, status="failed", progress=0, status_msg=f"Context error: {e}")
            return
        # Step 2: index
        _task_update(jid, progress=60, status_msg="Индексирование...")
        try:
            result = await asyncio.to_thread(rag_engine.index_project, project)
            _task_update(jid, status="completed", progress=100,
                              status_msg=f"Готово — проиндексировано {result['indexed_docs']} doc(s)")
            log_activity(project, "ctx_and_indexed", f"{result['indexed_docs']} docs")
        except Exception as e:
            _task_update(jid, status="failed", progress=60, status_msg=f"Index error: {e}")
        finally:
            cleanup_tasks()

    bg.add_task(do_ctx_then_index)
    log_activity(project, "ctx_and_index_queued", mode)
    return {"job_id": jid}

@app.get("/api/jobs", dependencies=[Depends(get_access)])
async def get_jobs():
    _sync_cache()
    return list(_tasks_cache.values())

@app.get("/download/{project}/{path:path}", dependencies=[Depends(get_access)])
async def download(project: str, path: str):
    projects_root_resolved = PROJECTS_ROOT.resolve()
    root_prefix = str(projects_root_resolved) + os.sep
    proj_path = (PROJECTS_ROOT / project).resolve()
    if not (str(proj_path) == str(projects_root_resolved) or str(proj_path).startswith(root_prefix)):
        raise HTTPException(400, "Invalid project")
    if not proj_path.exists():
        raise HTTPException(404)
    file_path = (proj_path / path).resolve()
    if not str(file_path).startswith(root_prefix):
        raise HTTPException(403, "Forbidden")
    if proj_path not in file_path.parents and file_path != proj_path:
        raise HTTPException(400, "Invalid path")
    if not file_path.exists():
        raise HTTPException(404)
    return FileResponse(file_path, filename=file_path.name)

@app.get("/balance")
async def balance():
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{cfg['llm']['openrouter']['base_url']}/credits", headers={"Authorization": f"Bearer {cfg['llm']['openrouter']['api_key']}"})
            return {"usd": r.json().get("total_credits", 0)}
    except: return {"error": "Check API key"}

# Запуск: uvicorn entrypoints.web_ui:app --host 0.0.0.0 --port 8000
