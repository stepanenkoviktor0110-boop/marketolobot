# entrypoints/web_ui.py (v3.0 + v4.0 Addons)
import os, sys, uuid, asyncio, logging, tempfile, json, base64, fcntl
from html import escape as html_escape
from pathlib import Path
from typing import Dict
from datetime import datetime, timedelta
import yaml
from fastapi import FastAPI, Form, BackgroundTasks, HTTPException, Depends, Request, Response
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional

# Core imports
from core.task_storage import (
    load_tasks,
    set_task as persist_set_task,
    update_task as persist_update_task,
    delete_task as persist_delete_task,
    cleanup_tasks as cleanup_tasks_persist,
)
from core.llm_client import llm_call

# === 1. CONFIG & PATHS ===
BASE_DIR = Path(__file__).resolve().parents[1]
with open(BASE_DIR / "config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

PROJECTS_ROOT = (BASE_DIR / cfg.get("projects_root", "projects")).resolve()
PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)

LLM_CFG = cfg.get("llm", {}).get("claude", cfg.get("llm", {}).get("openrouter", {}))
DRAFT_MODEL = LLM_CFG.get("draft_model", "haiku")
POLISH_MODEL = LLM_CFG.get("polish_model", "sonnet")

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
# auto_error=False lets us fall back to the cookie when no Bearer header is present.
security = HTTPBearer(auto_error=False)
AUTH_COOKIE = "pmf_auth"
AUTH_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _extract_token(
    credentials: Optional[HTTPAuthorizationCredentials],
    request: Request,
) -> Optional[str]:
    if credentials is not None:
        return credentials.credentials
    return request.cookies.get(AUTH_COOKIE)


async def get_access(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    t = _extract_token(credentials, request)
    if t not in (OWNER_TOKEN, SHARED_TOKEN):
        raise HTTPException(401, "Invalid token")
    return t


def require_owner(token: str = Depends(get_access)):
    if token != OWNER_TOKEN:
        raise HTTPException(403, "Owner access required")
    return token


def _classify_token(token: Optional[str]) -> Optional[str]:
    if token == OWNER_TOKEN:
        return "owner"
    if token == SHARED_TOKEN:
        return "shared"
    return None

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

# In-memory cache for tasks — used only as a read-through snapshot for
# /api/jobs. All mutations go through task_storage's locked R-M-W helpers
# so two uvicorn workers can't clobber each other's writes.
_tasks_cache: Dict[str, dict] = load_tasks()

def _sync_cache():
    global _tasks_cache
    _tasks_cache = load_tasks()

def _task_set(job_id: str, task_data: dict):
    persist_set_task(job_id, task_data)
    _sync_cache()

def _task_update(job_id: str, **kwargs):
    persist_update_task(job_id, **kwargs)
    _sync_cache()

def cleanup_tasks():
    cleanup_tasks_persist()
    _sync_cache()

async def execute_pipeline(job_id: str, stage: str, input_text: str, project_path: Path):
    _task_update(job_id, status="running", progress=15, status_msg="Drafting...")
    try:
        from core.router import run_stage
        res = await run_with_retry(run_stage, args=(stage, input_text, str(project_path)), max_retries=2)
        atomic_write(project_path / "output" / f"{stage}_final.md", res)
        _task_update(
            job_id,
            status="completed",
            progress=100,
            status_msg="Done",
            result_preview=res[:300],
            file=f"/view/{project_path.name}/output/{stage}_final.md",
        )
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
    res = await run_with_retry(_call_llm, args=(DRAFT_MODEL, prompt), kwargs={"call_site": "context_digest"}, max_retries=2)

    digest = p / "docs" / "context_digest.md"
    header = f"## 📅 {datetime.now().strftime('%Y-%m-%d %H:%M')} | Mode: {mode}\n\n"
    atomic_write(digest, header + res + "\n---\n" + (digest.read_text() if digest.exists() else ""))
    _task_update(job_id, status="completed", progress=100, status_msg="Context updated", file=f"/download/{project}/docs/context_digest.md")

async def _call_llm(model: str, prompt: str, *, call_site: str = "web_chat_full") -> str:
    """Async wrapper around the shared LLM dispatcher so retry/queueing keeps working."""
    content, *_ = await llm_call(call_site, prompt, model_override=model)
    return content

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
    # R-M-W under LOCK_EX so two workers don't drop each other's events.
    entry = {"time": datetime.now().strftime("%H:%M"), "project": project, "action": action, "details": details}
    with activity_path.open("r+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            raw = fh.read().strip()
            try:
                logs = json.loads(raw) if raw else []
            except json.JSONDecodeError:
                logs = []
            logs.append(entry)
            logs = logs[-100:]
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(logs, indent=2))
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

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
    return await run_with_retry(_call_llm, args=(DRAFT_MODEL, prompt), kwargs={"call_site": "web_chat_full"}, max_retries=2)

# === 6. V4.0 UI: Dashboard ===
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    # Cookie-session guard: unauthenticated → /login. Token is no longer
    # rendered into the page source; the browser sends it back as an
    # HttpOnly cookie on every fetch(), so even an XSS can't read it.
    if _classify_token(request.cookies.get(AUTH_COOKIE)) is None:
        return RedirectResponse("/login", status_code=303)
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
    circ_xl = round(2 * 3.14159265 * 46, 2)  # stroke-dasharray for r=46 → 289.03

    css = """
@import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;500;600;700&family=DM+Sans:ital,wght@0,300;0,400;0,500;0,600;1,400&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
  --bg:       #07090c;
  --surface:  #0d1117;
  --surface2: #111820;
  --border:   rgba(255,255,255,0.07);
  --borderhi: rgba(255,255,255,0.13);
  --text:     #dde1e6;
  --dim:      #9ea5ad;
  --faint:    #6d757f;
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
  font-size: 16px;
  line-height: 1.7;
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}

.wrap {
  max-width: 1100px;
  margin: 0 auto;
  padding: 64px 56px 120px;
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
  font-size: 48px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-hi, #f1e8d5);
  line-height: 1;
  margin-bottom: 12px;
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

/* ── SECTION NAV (tabs) ── */
.sec-nav {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 56px 0 48px;
  padding: 0;
  border-bottom: 1px solid var(--border);
  position: relative;
}
.sec-nav::before {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  top: 50%;
  height: 1px;
  background: var(--border);
  opacity: 0;
}
.sec-tab {
  background: none;
  border: none;
  padding: 20px 32px 22px;
  cursor: pointer;
  color: var(--dim);
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.24em;
  text-transform: uppercase;
  position: relative;
  margin-bottom: -1px;
  transition: color 0.2s;
  display: inline-flex;
  align-items: baseline;
  gap: 14px;
}
.sec-tab .num {
  font-family: var(--serif);
  font-style: italic;
  font-size: 22px;
  color: var(--faint);
  letter-spacing: 0;
  text-transform: none;
  transition: color 0.2s;
  line-height: 1;
}
.sec-tab:hover { color: var(--dim); }
.sec-tab:hover .num { color: var(--dim); }
.sec-tab.active { color: var(--text-hi, #f1e8d5); }
.sec-tab.active .num { color: var(--gold); }
.sec-tab.active::after {
  content: "";
  position: absolute;
  left: 28px;
  right: 28px;
  bottom: -1px;
  height: 2px;
  background: var(--gold);
}
.sec-panel { display: none; animation: secfade 0.35s ease-out; }
.sec-panel.active { display: block; }
@keyframes secfade {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: translateY(0); }
}

/* ── GRID ── */
.g2 { display: grid; grid-template-columns: 1fr 1fr; gap: 32px; margin-bottom: 32px; }

/* ── PMF HERO ── */
.pmf-hero {
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 40px;
  padding: 36px 40px;
  background: linear-gradient(135deg, rgba(200,164,92,0.05), transparent 60%), var(--surface);
  border: 1px solid var(--border);
  border-left: 2px solid var(--gold);
  margin-bottom: 32px;
}
.pmf-hero .hero-kicker {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.28em;
  text-transform: uppercase;
  color: var(--gold);
  margin-bottom: 6px;
}
.pmf-hero .hero-title {
  font-family: var(--serif);
  font-size: 2rem;
  line-height: 1.1;
  color: var(--text-hi, #f1e8d5);
  font-weight: 600;
}
.pmf-hero .hero-title em {
  font-style: italic;
  color: var(--gold);
  font-weight: 500;
}
.pmf-hero .hero-meta {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--dim);
  margin-top: 8px;
  letter-spacing: 0.04em;
}
.pmf-hero .ring-xl svg { width: 104px; height: 104px; }
.pmf-hero .ring-xl { position: relative; }
.pmf-hero .ring-xl .score-num {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--serif);
  font-size: 34px;
  font-weight: 600;
  color: var(--text-hi, #f1e8d5);
}
.pmf-hero .btn-refresh {
  background: none;
  border: 1px solid var(--gold-dim, rgba(200,164,92,0.35));
  color: var(--gold);
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  padding: 10px 22px;
  cursor: pointer;
  transition: background 0.2s, border-color 0.2s;
  white-space: nowrap;
}
.pmf-hero .btn-refresh:hover { background: var(--gold-bg); border-color: rgba(200,164,92,0.65); }

/* ── CARD ── */
.card { background: var(--surface); border: 1px solid var(--border); margin-bottom: 40px; }
.card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 28px 36px 20px;
  border-bottom: 1px solid var(--border);
  gap: 24px;
}
.card-title {
  font-family: var(--serif);
  font-size: 28px;
  font-weight: 600;
  letter-spacing: 0.01em;
  color: var(--text-hi, #f1e8d5);
  line-height: 1.1;
  position: relative;
  padding-left: 32px;
}
.card-title::before {
  content: "";
  position: absolute;
  left: 0;
  top: 0.55em;
  width: 20px;
  height: 1px;
  background: var(--gold);
}
.card-body { padding: 32px 36px 36px; }
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
select option {
  background: #0d1117;
  color: #f1e8d5;
  font-family: var(--sans);
  font-size: 14px;
  padding: 8px 12px;
}
select option:checked { background: rgba(200,164,92,0.15); color: var(--gold); }
select option:disabled { color: var(--faint); }
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
  max-height: 420px;
  overflow-y: auto;
  padding-right: 8px;
}
.log-wrap::-webkit-scrollbar { width: 3px; }
.log-wrap::-webkit-scrollbar-thumb { background: var(--borderhi); }
.log-row {
  display: grid;
  grid-template-columns: 72px 200px 1fr;
  gap: 24px;
  padding: 14px 0;
  border-bottom: 1px solid var(--border);
  align-items: baseline;
}
.log-row:last-child { border-bottom: none; }
.l-time  { color: var(--faint); font-family: var(--mono); font-size: 11px; letter-spacing: 0.06em; font-variant-numeric: tabular-nums; }
.l-proj  { color: var(--gold); font-family: var(--serif); font-style: italic; font-size: 16px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.l-msg   { color: var(--text); font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ── JOBS TABLE ── */
.j-table { width: 100%; border-collapse: collapse; }
.j-table th {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--gold);
  font-weight: 500;
  text-align: left;
  padding: 0 16px 16px 0;
  border-bottom: 1px solid var(--gold-dim, rgba(200,164,92,0.35));
}
.j-table th:last-child, .j-table td:last-child { padding-right: 0; text-align: right; }
.j-table td {
  padding: 18px 16px 18px 0;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
  font-size: 15px;
}
.j-table tbody tr { transition: background 0.2s; }
.j-table tbody tr:hover { background: rgba(200,164,92,0.03); }
.j-table tr:last-child td { border-bottom: none; }
.j-id { font-family: var(--mono); font-size: 12px; color: var(--faint); letter-spacing: 0.08em; }
.j-proj { font-family: var(--serif); font-size: 17px; color: var(--text); font-weight: 500; }
.s-pill {
  display: inline-block;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  padding: 5px 11px;
  border: 1px solid;
}
.s-done    { color: var(--green); border-color: rgba(76,175,125,0.4); }
.s-run     { color: var(--gold);  border-color: rgba(200,164,92,0.5); }
.s-queue   { color: var(--dim);   border-color: var(--borderhi); }
.s-fail    { color: var(--red);   border-color: rgba(224,92,78,0.45); }
.prog { width: 110px; height: 2px; background: rgba(255,255,255,0.08); display: inline-block; vertical-align: middle; }
.prog-f { height: 100%; background: var(--gold); transition: width 0.4s; }
.dl { font-family: var(--mono); font-size: 10px; letter-spacing: 0.18em; text-transform: uppercase; color: var(--gold); text-decoration: none; border-bottom: 1px solid var(--gold-dim, rgba(200,164,92,0.4)); padding-bottom: 2px; }
.dl:hover { border-bottom-color: var(--gold); }
.j-stage { font-family: var(--serif); font-style: italic; font-size: 16px; color: var(--text-hi, #f1e8d5); letter-spacing: 0.005em; }
.j-stage-dim { font-family: var(--mono); font-size: 11px; color: var(--faint); }
.btn-del {
  background: none;
  border: none;
  color: var(--faint);
  cursor: pointer;
  font-family: var(--mono);
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  line-height: 1;
  padding: 4px 8px;
  margin-left: 10px;
  transition: color 0.2s, letter-spacing 0.2s;
  position: relative;
}
.btn-del::before {
  content: "—";
  margin-right: 6px;
  color: var(--faint);
  transition: color 0.2s;
}
.btn-del:hover { color: var(--red); }
.btn-del:hover::before { color: var(--red); }
.btn-retry {
  background: none;
  border: none;
  color: var(--dim);
  cursor: pointer;
  font-family: var(--mono);
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  line-height: 1;
  padding: 4px 8px;
  margin-left: 10px;
  transition: color 0.2s;
  position: relative;
}
.btn-retry::before {
  content: "↻";
  margin-right: 6px;
  color: var(--faint);
  font-size: 12px;
  letter-spacing: 0;
  transition: color 0.2s, transform 0.2s;
  display: inline-block;
}
.btn-retry:hover { color: var(--gold); }
.btn-retry:hover::before { color: var(--gold); transform: rotate(180deg); }
.j-actions { white-space: nowrap; }

/* ── ARCHIVE ── */
.arch-sel {
  background: transparent;
  color: var(--text);
  border: none;
  border-bottom: 1px solid var(--border);
  padding: 6px 24px 6px 2px;
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  min-width: 220px;
  appearance: none;
  background-image: linear-gradient(45deg, transparent 50%, var(--gold) 50%), linear-gradient(-45deg, transparent 50%, var(--gold) 50%);
  background-position: calc(100% - 10px) calc(50% - 1px), calc(100% - 5px) calc(50% - 1px);
  background-size: 5px 5px;
  background-repeat: no-repeat;
  cursor: pointer;
}
.arch-sel:focus { outline: none; border-bottom-color: var(--gold); }
.arch-empty {
  font-family: var(--serif);
  font-style: italic;
  font-size: 14px;
  color: var(--faint);
  padding: 32px 0;
  text-align: center;
}
.arch-cat { margin-bottom: 40px; }
.arch-cat:last-child { margin-bottom: 0; }
.arch-cat-title {
  display: flex;
  align-items: baseline;
  gap: 14px;
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.28em;
  text-transform: uppercase;
  color: var(--gold);
  margin-bottom: 22px;
  padding-bottom: 0;
  border-bottom: none;
}
.arch-cat-title::before {
  content: "";
  display: inline-block;
  width: 24px;
  height: 1px;
  background: var(--gold);
  transform: translateY(-3px);
}
.arch-count {
  color: var(--faint);
  margin-left: 0;
  letter-spacing: 0;
  font-size: 12px;
  font-family: var(--serif);
  font-style: italic;
}
.arch-list { list-style: none; padding: 0; margin: 0; }
.arch-item {
  display: grid;
  grid-template-columns: 1fr auto auto;
  grid-template-areas: "link meta del" "path path del";
  align-items: center;
  gap: 2px 28px;
  padding: 22px 20px 22px 24px;
  margin-left: -24px;
  border-bottom: none;
  position: relative;
  transition: background 0.2s;
  animation: archrise 0.5s both;
  animation-delay: calc(var(--i, 0) * 30ms);
}
@keyframes archrise {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: translateY(0); }
}
.arch-item::before {
  content: "";
  position: absolute;
  left: 0; top: 10px; bottom: 10px;
  width: 1px;
  background: transparent;
  transition: background 0.2s, width 0.2s;
}
.arch-item:hover { background: rgba(200,164,92,0.03); }
.arch-item:hover::before { background: var(--gold); width: 2px; }
.arch-del { grid-area: del; margin-left: 0; }
.arch-link {
  grid-area: link;
  color: var(--text-hi, #f1e8d5);
  text-decoration: none;
  font-size: 19px;
  font-family: var(--serif);
  font-weight: 500;
  letter-spacing: 0.005em;
  line-height: 1.25;
  transition: color 0.15s;
}
.arch-link:hover { color: var(--gold); }
.arch-meta {
  grid-area: meta;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--dim);
  letter-spacing: 0.06em;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.arch-path {
  grid-area: path;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--faint);
  letter-spacing: 0.04em;
  margin-top: 4px;
}

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

    # No token in page source: cookie-session auth. fetch() is same-origin so
    # the pmf_auth HttpOnly cookie rides along automatically.
    stage_labels_json = json.dumps({k: v[0] for k, v in STAGE_META.items()}, ensure_ascii=False)
    js_auth = (
        "const headers = () => ({ 'Content-Type': 'application/json' });\n"
        f"const STAGE_LABELS = {stage_labels_json};"
    )

    js_body = r"""
// Auto-redirect to /login on 401 — cookie may have expired or been revoked.
(function wrapFetch() {
  const orig = window.fetch;
  window.fetch = async (...args) => {
    const r = await orig.apply(this, args);
    if (r.status === 401) { location.href = '/login'; throw new Error('unauthorized'); }
    return r;
  };
})();

async function logout() {
  try { await fetch('/logout', { method: 'POST' }); } catch (e) {}
  location.href = '/login';
}

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

async function loadScore(opts) {
  const p = document.getElementById('projSelect1').value || document.getElementById('projSelect2').value;
  if (!p) {
    // Silent: setInterval calls this every 10s and must not spam alerts.
    if (opts && opts.interactive) alert('Сначала выбери проект');
    return;
  }
  const r = await fetch('/api/pmf_score?project=' + encodeURIComponent(p), { headers: headers() });
  const d = await r.json();
  const pct = d.score;
  const arc = document.getElementById('scoreArc');
  if (arc) {
    const r = Number(arc.getAttribute('r')) || 26;
    const circumference = 2 * Math.PI * r;
    arc.setAttribute('stroke-dasharray', circumference);
    arc.style.strokeDashoffset = circumference - (pct / 100) * circumference;
  }
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

let _lastActivityKey = '';
async function loadActivity() {
  try {
    const r = await fetch('/api/activity', { headers: headers() });
    const d = await r.json();
    const feed = document.getElementById('activityFeed');
    if (!d.length) return;
    const rows = d.slice(-30).reverse();
    const key = rows.map(x => [x.time, x.project || '', x.action, x.details || ''].join('|')).join(';');
    if (key === _lastActivityKey) return;
    _lastActivityKey = key;
    feed.innerHTML = rows.map(x =>
      '<div class="log-row">' +
      '<span class="l-time">' + x.time + '</span>' +
      '<span class="l-proj">' + (x.project || '\u2014') + '</span>' +
      '<span class="l-msg">' + x.action + (x.details ? ' \u00b7 ' + x.details : '') + '</span>' +
      '</div>'
    ).join('');
  } catch(e) { /* 401 handled by global fetch wrapper */ }
}

async function loadBalance() {
  const mainEl = document.getElementById('balanceMain');
  const subEl = document.getElementById('balanceSub');
  if (!mainEl || !subEl) return;
  mainEl.textContent = 'Claude Code · подписка';
  subEl.textContent = 'Без оплаты по токенам';
  mainEl.classList.remove('low', 'danger');
}

function setSection(name) {
  document.querySelectorAll('.sec-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.sec === name);
    t.setAttribute('aria-selected', t.dataset.sec === name ? 'true' : 'false');
  });
  document.querySelectorAll('.sec-panel').forEach(p => {
    p.classList.toggle('active', p.dataset.sec === name);
  });
  try { localStorage.setItem('pmf.section', name); } catch (e) {}
  if (name === 'team') { loadGuests(); loadActivity(); }
  if (name === 'results') { loadTasks(); }
}

async function retryJob(jobId) {
  if (!confirm('Перезапустить этап с теми же параметрами?')) return;
  try {
    const r = await fetch('/api/jobs/' + encodeURIComponent(jobId) + '/retry', { method: 'POST', headers: headers() });
    if (!r.ok) {
      const msg = await r.text();
      alert('Не удалось повторить: ' + r.status + (msg ? ' — ' + msg.slice(0, 200) : ''));
      return;
    }
    loadTasks();
  } catch (e) { alert('Ошибка: ' + e); }
}

async function deleteJob(jobId) {
  if (!confirm('Удалить задачу из списка? Файл результата останется в Архиве.')) return;
  try {
    const r = await fetch('/api/jobs/' + encodeURIComponent(jobId), { method: 'DELETE', headers: headers() });
    if (!r.ok) { alert('Не удалось удалить: ' + r.status); return; }
    loadTasks();
  } catch (e) { alert('Ошибка: ' + e); }
}

async function deleteArchiveFile(proj, path) {
  if (!confirm('Удалить файл ' + path + '? Действие необратимо.')) return;
  const pathEnc = path.split('/').map(encodeURIComponent).join('/');
  try {
    const r = await fetch('/api/archive/' + encodeURIComponent(proj) + '/' + pathEnc, { method: 'DELETE', headers: headers() });
    if (!r.ok) { alert('Не удалось удалить: ' + r.status); return; }
    loadArchive();
  } catch (e) { alert('Ошибка: ' + e); }
}

async function openTaskFile(url) {
  try {
    // Auth via HttpOnly cookie — no bearer header needed.
    const r = await fetch(url);
    if (r.status === 401) { location.href = '/login'; return; }
    if (!r.ok) { alert('Не удалось открыть: ' + r.status); return; }
    const blob = await r.blob();
    const blobUrl = URL.createObjectURL(blob);
    // noopener severs window.opener so a sanitizer-bypass XSS in the
    // rendered .md cannot read the parent's TOKEN.
    window.open(blobUrl, '_blank', 'noopener,noreferrer');
    setTimeout(() => URL.revokeObjectURL(blobUrl), 60000);
  } catch (e) { alert('Ошибка: ' + e); }
}

let _lastTasksKey = '';
async function loadTasks() {
  try {
    const r = await fetch('/api/jobs', { headers: headers() });
    const j = await r.json();
    // Skip re-render when nothing observable changed — keeps button
    // hover/focus/tooltips alive across the 2s poll interval.
    const visible = j.slice(-20).reverse();
    const key = visible.map(x => [x.id, x.status, x.progress, x.file || '', x.input ? 1 : 0].join('|')).join(';');
    if (key === _lastTasksKey) return;
    _lastTasksKey = key;
    const cls = { completed: 's-done', running: 's-run', queued: 's-queue', failed: 's-fail' };
    document.getElementById('tBody').innerHTML = visible.map(x => {
      const stageCode = x.stage || '';
      const stageLabel = STAGE_LABELS[stageCode] || (stageCode ? stageCode : '—');
      const stageCell = stageCode
        ? '<span class="j-stage" title="' + escapeHtml(stageCode) + '">' + escapeHtml(stageLabel) + '</span>'
        : '<span class="j-stage-dim">—</span>';
      return '<tr>' +
      '<td class="j-id">' + x.id.slice(0, 8) + '</td>' +
      '<td class="j-proj">' + x.project + '</td>' +
      '<td>' + stageCell + '</td>' +
      '<td><span class="s-pill ' + (cls[x.status] || 's-queue') + '">' + x.status + '</span></td>' +
      '<td><div class="prog"><div class="prog-f" style="width:' + x.progress + '%"></div></div></td>' +
      '<td class="j-actions">' +
        (x.file ? '<a href="#" onclick="openTaskFile(\'' + x.file + '\');return false;" class="dl">Открыть</a>' : '') +
        // Retry only if we have stage + non-empty input captured (back-filled
        // rows without input would re-run with "" and produce garbage).
        ((x.stage && x.input && x.status !== 'running' && x.status !== 'queued') ? ' <button type="button" class="btn-retry" title="Повторить с теми же параметрами" onclick="retryJob(\'' + x.id + '\')">Повторить</button>' : '') +
        ' <button type="button" class="btn-del" title="Удалить запись" onclick="deleteJob(\'' + x.id + '\')">Удалить</button>' +
      '</td>' +
      '</tr>';
    }).join('');
  } catch(e) { /* 401 handled by global fetch wrapper */ }
}

function formatArchTs(ts) {
  const d = new Date(ts * 1000);
  const date = d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
  const time = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  return date + ', ' + time;
}

async function loadArchive() {
  const sel = document.getElementById('archSelect');
  const body = document.getElementById('archBody');
  if (!sel || !body) return;
  const proj = sel.value;
  if (!proj) {
    body.innerHTML = '<div class="arch-empty">Выбери проект, чтобы увидеть все документы</div>';
    return;
  }
  body.innerHTML = '<div class="arch-empty">Загрузка…</div>';
  try {
    const r = await fetch('/api/archive/' + encodeURIComponent(proj), { headers: headers() });
    if (!r.ok) { body.innerHTML = '<div class="arch-empty">Ошибка ' + r.status + '</div>'; return; }
    const j = await r.json();
    if (!j.files || !j.files.length) {
      body.innerHTML = '<div class="arch-empty">Нет документов</div>';
      return;
    }
    const labels = {
      output: 'Результаты этапов',
      hypotheses: 'Гипотезы (/hypothesize)',
      brainstorm: 'Брейншторм (/brainstorm)',
      ratings: 'Оценки (/rate)',
      docs: 'Контекст / дайджесты',
      inbox: 'Голосовые заметки',
      root: 'Корень проекта',
    };
    const order = ['output','hypotheses','brainstorm','ratings','docs','inbox','root'];
    const groups = {};
    j.files.forEach(f => { (groups[f.category] = groups[f.category] || []).push(f); });
    const cats = Object.keys(groups).sort((a, b) => {
      const ia = order.indexOf(a), ib = order.indexOf(b);
      return (ia === -1 ? 999 : ia) - (ib === -1 ? 999 : ib);
    });
    const projEnc = encodeURIComponent(proj);
    body.innerHTML = cats.map(c => {
      const items = groups[c].map((f, i) => {
        const pathEnc = f.path.split('/').map(encodeURIComponent).join('/');
        const url = '/view/' + projEnc + '/' + pathEnc;
        const kb = (f.size / 1024).toFixed(1) + ' KB';
        const pathJson = JSON.stringify(f.path);
        const projJson = JSON.stringify(proj);
        return '<li class="arch-item" style="--i:' + i + '">' +
          '<a href="#" onclick="openTaskFile(\'' + url + '\');return false;" class="arch-link">' + escapeHtml(f.name) + '</a>' +
          '<span class="arch-meta">' + formatArchTs(f.mtime) + ' · ' + kb + '</span>' +
          '<span class="arch-path">' + escapeHtml(f.path) + '</span>' +
          '<button type="button" class="btn-del arch-del" title="Удалить файл" onclick="deleteArchiveFile(' + escapeHtml(projJson) + ',' + escapeHtml(pathJson) + ')">Удалить</button>' +
          '</li>';
      }).join('');
      return '<div class="arch-cat">' +
        '<div class="arch-cat-title">' + (labels[c] || c) + ' <span class="arch-count">' + groups[c].length + '</span></div>' +
        '<ul class="arch-list">' + items + '</ul>' +
        '</div>';
    }).join('');
  } catch (e) {
    body.innerHTML = '<div class="arch-empty">Ошибка: ' + e + '</div>';
  }
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
  // Only poll PMF score when Results section is visible — saves a
  // pointless /api/pmf_score hit every 10s on the Work/Team tabs.
  setInterval(() => {
    const panel = document.querySelector('.sec-panel[data-sec="results"]');
    if (panel && panel.classList.contains('active')) loadScore();
  }, 10000);

  try {
    const lastSec = localStorage.getItem('pmf.section');
    if (lastSec && ['work','results','team'].includes(lastSec)) setSection(lastSec);
  } catch (e) {}
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
          <div class="balance-main" id="balanceMain">Claude Code · подписка</div>
        </div>
        <div class="balance-sub" id="balanceSub">Без оплаты по токенам</div>
      </div>
      <div class="owner-badge">Личный ассистент</div>
      <span class="v-badge">v4.0</span>
      <button type="button" class="t-btn-action" onclick="logout()" title="Сбросить сессию">Выход</button>
    </div>
  </header>

  <div class="toolbar">
    <button class="t-btn" onclick="toggleChat()">AI-чат</button>
    <button class="t-btn" id="btnVoice" onclick="toggleRecord()">Голос</button>
    <button class="t-btn-action" onclick="reindexProject()" title="Только индексация без обработки контекста">Только индексировать</button>
  </div>

  <nav class="sec-nav" id="secNav" role="tablist">
    <button class="sec-tab active" data-sec="work"    onclick="setSection('work')"    role="tab"><span class="num">I</span>Работа</button>
    <button class="sec-tab"        data-sec="results" onclick="setSection('results')" role="tab"><span class="num">II</span>Результаты</button>
    <button class="sec-tab"        data-sec="team"    onclick="setSection('team')"    role="tab"><span class="num">III</span>Команда</button>
  </nav>

  <section class="sec-panel active" data-sec="work">
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
        </div>
      </div>
    </div>
  </section>

  <section class="sec-panel" data-sec="results">
    <div class="pmf-hero">
      <div class="ring-xl">
        <svg width="104" height="104" viewBox="0 0 104 104">
          <circle cx="52" cy="52" r="46" fill="none" stroke="rgba(255,255,255,0.05)" stroke-width="3"/>
          <circle id="scoreArc" cx="52" cy="52" r="46" fill="none" stroke="#c8a45c" stroke-width="3"
            stroke-linecap="butt"
            stroke-dasharray="{circ_xl}"
            stroke-dashoffset="{circ_xl}"
            transform="rotate(-90 52 52)"/>
        </svg>
        <div class="score-num" id="scoreNum">—</div>
      </div>
      <div>
        <div class="hero-kicker">PMF Readiness</div>
        <div class="hero-title">Готовность <em>продуктово-рыночного</em> соответствия</div>
        <div class="hero-meta" id="scoreMeta">Выбери проект и нажми «Пересчитать»</div>
      </div>
      <button type="button" class="btn-refresh" onclick="loadScore({{interactive:true}})">Пересчитать</button>
    </div>

    <div class="card">
      <div class="card-head"><div class="card-title">Задачи</div></div>
      <div class="card-body">
        <table class="j-table">
          <thead>
            <tr>
              <th>ID</th><th>Проект</th><th>Этап</th><th>Статус</th><th>Прогресс</th><th>Файл</th>
            </tr>
          </thead>
          <tbody id="tBody"></tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <div class="card-title">Архив документов</div>
        <select id="archSelect" onchange="loadArchive()" class="arch-sel">{proj_opts}</select>
      </div>
      <div class="card-body" id="archBody">
        <div class="arch-empty">Выбери проект, чтобы увидеть все документы</div>
      </div>
    </div>
  </section>

  <section class="sec-panel" data-sec="team">
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
  </section>

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

LOGIN_PAGE_TEMPLATE = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<title>PMF Pipeline — вход</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root {{
  --bg:#07090c; --surface:#0d1117; --border:rgba(255,255,255,0.09);
  --borderhi:rgba(255,255,255,0.18); --text:#dde1e6; --dim:#9ea5ad;
  --faint:#6d757f; --gold:#c8a45c; --red:#e05c4e;
  --serif:'Cormorant Garamond',Georgia,serif;
  --sans:'DM Sans',system-ui,sans-serif;
  --mono:'JetBrains Mono',monospace;
}}
*,*::before,*::after {{ box-sizing:border-box; margin:0; padding:0; }}
body {{
  font-family:var(--sans); background:var(--bg); color:var(--text);
  min-height:100vh; display:flex; align-items:center; justify-content:center;
  font-size:15px; line-height:1.6;
  -webkit-font-smoothing:antialiased;
}}
.card {{
  width:100%; max-width:420px; padding:48px 40px;
  background:var(--surface); border:1px solid var(--border);
  border-left:2px solid var(--gold);
}}
.kicker {{
  font-family:var(--mono); font-size:10px; letter-spacing:0.28em;
  text-transform:uppercase; color:var(--gold); margin-bottom:14px;
}}
h1 {{
  font-family:var(--serif); font-weight:600; font-size:36px;
  color:#f1e8d5; margin-bottom:6px; line-height:1.1;
}}
.sub {{
  font-family:var(--serif); font-style:italic; color:var(--dim);
  margin-bottom:36px;
}}
label {{
  display:block; font-family:var(--mono); font-size:10px;
  letter-spacing:0.2em; text-transform:uppercase; color:var(--faint);
  margin-bottom:8px;
}}
input[type=password] {{
  width:100%; padding:12px 15px; background:#060809;
  border:1px solid var(--borderhi); color:var(--text);
  font-family:var(--mono); font-size:14px; outline:none;
  transition:border-color 0.15s;
}}
input[type=password]:focus {{ border-color:var(--gold); }}
button {{
  width:100%; margin-top:22px; padding:14px 24px;
  background:var(--gold); color:#07090c; border:none;
  font-family:var(--sans); font-weight:600; font-size:12px;
  letter-spacing:0.13em; text-transform:uppercase; cursor:pointer;
  transition:opacity 0.15s;
}}
button:hover {{ opacity:0.84; }}
.err {{
  margin-top:16px; color:var(--red); font-size:13px;
  font-family:var(--mono);
}}
.hint {{
  margin-top:24px; font-size:12px; color:var(--faint);
  line-height:1.5;
}}
</style>
</head><body>
<form class="card" method="POST" action="/login">
  <div class="kicker">PMF Pipeline</div>
  <h1>Вход</h1>
  <div class="sub">введите токен доступа</div>
  <label for="token">Token</label>
  <input id="token" type="password" name="token" autofocus autocomplete="current-password">
  <button type="submit">Войти</button>
  {error_block}
  <div class="hint">owner или shared token из config.yaml webui.*_token</div>
</form>
</body></html>"""


def _render_login(error: Optional[str] = None) -> str:
    err_html = (
        f'<div class="err">{html_escape(error)}</div>' if error else ""
    )
    return LOGIN_PAGE_TEMPLATE.format(error_block=err_html)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # Already logged in → bounce straight to dashboard.
    if _classify_token(request.cookies.get(AUTH_COOKIE)) is not None:
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_render_login())


@app.post("/login")
async def do_login(request: Request, token: str = Form(...)):
    if _classify_token(token) is None:
        # Constant-time comparison is not necessary here — tokens are 64-char
        # hex; the leak surface is the form field itself if mistyped.
        return HTMLResponse(_render_login("Неверный токен"), status_code=401)
    resp = RedirectResponse("/", status_code=303)
    secure_cookie = request.url.scheme == "https" or request.headers.get(
        "x-forwarded-proto", ""
    ).lower() == "https"
    resp.set_cookie(
        AUTH_COOKIE,
        token,
        max_age=AUTH_COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=secure_cookie,
        path="/",
    )
    return resp


@app.post("/logout")
async def do_logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(AUTH_COOKIE, path="/")
    return resp


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
            response = await run_with_retry(_call_llm, args=(DRAFT_MODEL, prompt), kwargs={"call_site": "web_chat_rag"}, max_retries=2)
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
        return {"ok": True, "mode": "subscription", "provider": "claude-code"}
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
    feedback_file = BASE_DIR / "data" / "bot_feedback.md"
    feedback_file.parent.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = (
        f"## [{ts}] | 🌐 webui\n"
        f"**Контекст:** Web UI\n"
        f"**Замечание:** {text}\n"
        f"**Решение:** —\n\n"
        f"---\n\n"
    )
    # Prepend under LOCK_EX so concurrent writes don't drop entries.
    if not feedback_file.exists():
        feedback_file.touch()
    with feedback_file.open("r+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            existing = fh.read()
            fh.seek(0)
            fh.truncate()
            fh.write(entry + existing)
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
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
    _task_set(jid, {"id": jid, "status": "queued", "progress": 0, "stage": stage, "project": project, "input": input, "file": None})
    bg.add_task(execute_pipeline, jid, stage, input, p)
    log_activity(project, "stage_queued", stage)
    return {"job_id": jid}

@app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(require_owner)])
async def retry_job(bg: BackgroundTasks, job_id: str):
    # Fresh read — the other worker may have just created this job.
    t = load_tasks().get(job_id)
    if not t:
        raise HTTPException(404, "Job not found")
    stage = t.get("stage")
    project = t.get("project")
    user_input = t.get("input", "")
    if not stage or not project:
        raise HTTPException(400, "Retry supported only for stage runs")
    if not user_input:
        raise HTTPException(400, "Исходный input не сохранён у этой задачи — повторить невозможно")
    p = PROJECTS_ROOT / project
    if not p.exists():
        raise HTTPException(404, "Project not found")
    new_jid = str(uuid.uuid4())
    _task_set(new_jid, {
        "id": new_jid, "status": "queued", "progress": 0,
        "stage": stage, "project": project, "input": user_input,
        "file": None, "retry_of": job_id,
    })
    bg.add_task(execute_pipeline, new_jid, stage, user_input, p)
    log_activity(project, "stage_retried", f"{stage} from {job_id[:8]}")
    return {"job_id": new_jid}

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

@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_owner)])
async def delete_job(job_id: str):
    # Fresh read — atomic delete under lock so we don't race sibling worker.
    current = load_tasks().get(job_id)
    if current is None:
        raise HTTPException(404, "Job not found")
    proj = current.get("project", "")
    if not persist_delete_task(job_id):
        raise HTTPException(404, "Job not found")
    _sync_cache()
    log_activity(proj, "job_deleted", job_id[:8])
    return {"ok": True}

@app.delete("/api/archive/{project}/{path:path}", dependencies=[Depends(require_owner)])
async def delete_archive_file(project: str, path: str):
    file_path = _resolve_project_file(project, path)
    if file_path.suffix.lower() != ".md":
        raise HTTPException(400, "Only .md files can be deleted")
    if not file_path.is_file():
        raise HTTPException(404, "Not a regular file")
    file_path.unlink()
    log_activity(project, "file_deleted", path)
    return {"ok": True}

def _resolve_project_file(project: str, path: str) -> Path:
    # Defence-in-depth: reject empty / dot / separator-bearing project segments
    # before pathlib can normalize them into something unexpected.
    if not project or project in (".", "..") or "/" in project or "\\" in project:
        raise HTTPException(400, "Invalid project")
    projects_root_resolved = PROJECTS_ROOT.resolve()
    root_prefix = str(projects_root_resolved) + os.sep
    proj_path = (PROJECTS_ROOT / project).resolve()
    if not str(proj_path).startswith(root_prefix):
        raise HTTPException(400, "Invalid project")
    if not proj_path.is_dir():
        raise HTTPException(404)
    file_path = (proj_path / path).resolve()
    if not str(file_path).startswith(root_prefix):
        raise HTTPException(403, "Forbidden")
    if proj_path not in file_path.parents and file_path != proj_path:
        raise HTTPException(400, "Invalid path")
    if not file_path.exists():
        raise HTTPException(404)
    return file_path


@app.get("/download/{project}/{path:path}", dependencies=[Depends(get_access)])
async def download(project: str, path: str):
    file_path = _resolve_project_file(project, path)
    return FileResponse(file_path, filename=file_path.name)

_ARCHIVE_CATEGORIES = ("output", "hypotheses", "brainstorm", "ratings", "docs", "inbox")


@app.get("/api/archive/{project}", dependencies=[Depends(get_access)])
async def archive(project: str):
    if not project or project in (".", "..") or "/" in project or "\\" in project:
        raise HTTPException(400, "Invalid project")
    projects_root_resolved = PROJECTS_ROOT.resolve()
    root_prefix = str(projects_root_resolved) + os.sep
    proj_path = (PROJECTS_ROOT / project).resolve()
    if not str(proj_path).startswith(root_prefix):
        raise HTTPException(400, "Invalid project")
    if not proj_path.is_dir():
        raise HTTPException(404)
    items: list[dict] = []

    def _push(f: Path, category: str):
        try:
            rel = f.relative_to(proj_path).as_posix()
        except ValueError:
            return
        items.append({
            "name": f.name,
            "path": rel,
            "category": category,
            "mtime": f.stat().st_mtime,
            "size": f.stat().st_size,
        })

    # Whitelisted artifact directories — shallow recursion stays inside them
    # so hidden dirs (.claude, .git, .venv) never leak into the archive list.
    for sub in _ARCHIVE_CATEGORIES:
        sub_path = proj_path / sub
        if not sub_path.is_dir():
            continue
        for f in sub_path.rglob("*.md"):
            if f.is_file():
                _push(f, sub)

    # Root-level .md files (project_summary.md, notes.md, …).
    for f in proj_path.glob("*.md"):
        if f.is_file():
            _push(f, "root")

    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"project": project, "files": items}

@app.get("/view/{project}/{path:path}", dependencies=[Depends(get_access)])
async def view_file(project: str, path: str):
    file_path = _resolve_project_file(project, path)
    raw = file_path.read_text(encoding="utf-8")
    title = html_escape(f"{project} / {path}")
    content_json = json.dumps(raw)
    # Split project and path for the masthead display
    try:
        project_disp, path_disp = title.split(" / ", 1)
    except ValueError:
        project_disp, path_disp = title, ""
    html = f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,500;0,600;0,700;1,500&family=DM+Sans:ital,wght@0,400;0,500;1,400&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"
        integrity="sha384-/TQbtLCAerC3jgaim+N78RZSDYV7ryeoBCVqTuzRrFec2akfBkHS7ACQ3PQhvMVi"
        crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.0.9/dist/purify.min.js"
        integrity="sha384-3HPB1XT51W3gGRxAmZ+qbZwRpRlFQL632y8x+adAqCr4Wp3TaWwCLSTAJJKbyWEK"
        crossorigin="anonymous"></script>
<style>
:root {{
  --bg:#07090c;
  --surface:#0d1117;
  --border:rgba(255,255,255,0.07);
  --text:#d7dbe0;
  --text-hi:#f1e8d5;
  --dim:#9ea5ad;
  --faint:#6d757f;
  --gold:#c8a45c;
  --gold-dim:rgba(200,164,92,0.35);
  --serif:'Cormorant Garamond',Georgia,serif;
  --sans:'DM Sans',system-ui,sans-serif;
  --mono:'JetBrains Mono',monospace;
}}
*,*::before,*::after {{ box-sizing:border-box; margin:0; padding:0; }}
html {{ scroll-behavior:smooth; }}
body {{
  font-family:var(--sans);
  background:var(--bg);
  color:var(--text);
  font-size:16px;
  line-height:1.72;
  -webkit-font-smoothing:antialiased;
  min-height:100vh;
  position:relative;
  overflow-x:hidden;
}}
/* Atmospheric backdrop — gradient + faint grain, no solid flatness */
body::before {{
  content:"";
  position:fixed;
  inset:0;
  background:
    radial-gradient(ellipse 1200px 600px at 15% -10%, rgba(200,164,92,0.06), transparent 70%),
    radial-gradient(ellipse 1000px 800px at 85% 110%, rgba(200,164,92,0.03), transparent 60%);
  pointer-events:none;
  z-index:0;
}}
body::after {{
  content:"";
  position:fixed;
  inset:0;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='200' height='200'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0.035 0'/></filter><rect width='100%25' height='100%25' filter='url(%23n)'/></svg>");
  opacity:0.5;
  pointer-events:none;
  z-index:0;
  mix-blend-mode:overlay;
}}
.wrap {{
  position:relative;
  z-index:1;
  max-width:720px;
  margin:0 auto;
  padding:72px 40px 140px;
  animation:rise 0.7s cubic-bezier(0.2,0.8,0.2,1) both;
}}
@keyframes rise {{
  from {{ opacity:0; transform:translateY(12px); }}
  to   {{ opacity:1; transform:translateY(0); }}
}}
/* Masthead — editorial-style, asymmetric */
.mast {{
  display:grid;
  grid-template-columns:auto 1fr auto;
  align-items:baseline;
  gap:20px;
  padding-bottom:24px;
  margin-bottom:64px;
  position:relative;
}}
.mast::after {{
  content:"";
  position:absolute;
  left:0; bottom:0;
  width:72px;
  height:1px;
  background:var(--gold);
}}
.mast::before {{
  content:"";
  position:absolute;
  left:72px; bottom:0;
  right:0;
  height:1px;
  background:var(--border);
}}
.kicker {{
  font-family:var(--mono);
  font-size:10px;
  letter-spacing:0.32em;
  text-transform:uppercase;
  color:var(--gold);
}}
.slug {{
  font-family:var(--serif);
  font-style:italic;
  font-size:16px;
  color:var(--dim);
  white-space:nowrap;
  overflow:hidden;
  text-overflow:ellipsis;
}}
.nav-back {{
  font-family:var(--mono);
  font-size:10px;
  letter-spacing:0.2em;
  text-transform:uppercase;
  color:var(--dim);
  text-decoration:none;
  white-space:nowrap;
  transition:color 0.2s;
  cursor:pointer;
  background:none;
  border:none;
  padding:0;
}}
.nav-back:hover {{ color:var(--gold); }}
.nav-back .arr {{ display:inline-block; transition:transform 0.2s; }}
.nav-back:hover .arr {{ transform:translateX(-3px); }}
/* Content — editorial reading surface */
.md {{ font-size:17px; }}
.md > *:first-child {{ margin-top:0; }}
.md h1, .md h2, .md h3, .md h4 {{
  font-family:var(--serif);
  color:var(--text-hi);
  font-weight:600;
  line-height:1.2;
}}
.md h1 {{
  font-size:2.5rem;
  margin:0 0 0.5em;
  letter-spacing:-0.01em;
  position:relative;
  padding-top:0.35em;
}}
.md h1::before {{
  content:"";
  display:block;
  position:absolute;
  top:0; left:0;
  width:48px;
  height:2px;
  background:var(--gold);
}}
.md h2 {{
  font-size:1.75rem;
  color:var(--gold);
  margin:2em 0 0.5em;
  font-style:italic;
  font-weight:500;
}}
.md h3 {{
  font-size:1.3rem;
  margin:1.6em 0 0.4em;
  color:var(--text-hi);
}}
.md h4 {{
  font-size:0.82rem;
  font-family:var(--mono);
  letter-spacing:0.16em;
  text-transform:uppercase;
  color:var(--dim);
  margin:1.8em 0 0.5em;
  font-weight:500;
}}
.md p {{ margin:1.1em 0; }}
/* Editorial drop-cap on the very first paragraph after H1 */
.md h1 + p::first-letter {{
  font-family:var(--serif);
  font-size:4em;
  line-height:0.85;
  float:left;
  color:var(--gold);
  padding:0.08em 0.1em 0 0;
  font-weight:600;
}}
.md ul, .md ol {{ margin:1em 0 1em 1.6em; }}
.md li {{ margin:0.4em 0; padding-left:0.2em; }}
.md ul > li::marker {{ color:var(--gold); content:"— "; }}
.md ol > li::marker {{ color:var(--gold); font-family:var(--mono); font-size:0.9em; }}
.md code {{
  font-family:var(--mono);
  font-size:0.85em;
  background:rgba(255,255,255,0.04);
  padding:2px 6px;
  border:1px solid var(--border);
  border-radius:2px;
  color:var(--gold);
}}
.md pre {{
  background:var(--surface);
  border:1px solid var(--border);
  border-left:2px solid var(--gold-dim);
  border-radius:0;
  padding:18px 20px;
  overflow-x:auto;
  margin:1.4em 0;
  font-size:13px;
}}
.md pre code {{ padding:0; background:transparent; border:none; color:var(--text); }}
.md blockquote {{
  margin:1.4em 0;
  padding:0.3em 0 0.3em 1.4em;
  border-left:2px solid var(--gold);
  color:var(--dim);
  font-family:var(--serif);
  font-style:italic;
  font-size:1.1em;
  line-height:1.55;
}}
.md hr {{
  border:none;
  height:1px;
  background:linear-gradient(90deg, transparent, var(--border) 20%, var(--border) 80%, transparent);
  margin:3em auto;
  max-width:80%;
}}
.md table {{
  border-collapse:collapse;
  margin:1.4em 0;
  width:100%;
  font-size:14px;
}}
.md th, .md td {{
  border-bottom:1px solid var(--border);
  padding:10px 14px;
  text-align:left;
  vertical-align:top;
}}
.md th {{
  font-family:var(--mono);
  font-size:10px;
  letter-spacing:0.16em;
  text-transform:uppercase;
  color:var(--gold);
  border-bottom:1px solid var(--gold-dim);
  font-weight:500;
}}
.md a {{
  color:var(--gold);
  text-decoration:none;
  border-bottom:1px solid var(--gold-dim);
  transition:border-color 0.2s;
}}
.md a:hover {{ border-bottom-color:var(--gold); }}
.md strong {{ color:var(--text-hi); font-weight:600; }}
.md em {{ font-family:var(--serif); font-style:italic; color:var(--text-hi); font-size:1.05em; }}
.foot {{
  margin-top:100px;
  padding-top:24px;
  border-top:1px solid var(--border);
  font-family:var(--mono);
  font-size:10px;
  letter-spacing:0.2em;
  text-transform:uppercase;
  color:var(--faint);
  display:flex;
  justify-content:space-between;
}}
@media (max-width:680px) {{
  .wrap {{ padding:48px 24px 100px; }}
  .mast {{ grid-template-columns:1fr auto; }}
  .slug {{ grid-column:1 / -1; }}
  .md h1 {{ font-size:2rem; }}
  .md h1 + p::first-letter {{ font-size:3em; }}
}}
</style>
</head><body>
<div class="wrap">
  <header class="mast">
    <span class="kicker">{html_escape(project_disp)}</span>
    <span class="slug">{html_escape(path_disp)}</span>
    <button type="button" class="nav-back" onclick="goBack()"><span class="arr">←</span> к панели</button>
  </header>
  <article class="md" id="md"></article>
  <div class="foot">
    <span>Маркетбот · PMF pipeline</span>
    <span>{path_disp.rsplit('/', 1)[-1] if '/' in path_disp else path_disp}</span>
  </div>
</div>
<script>
const raw = {content_json};
// Sanitize: .md files come from LLM output / voice transcripts / group chat —
// any of which can contain raw <script> and steal window.opener.TOKEN.
const rendered = marked.parse(raw, {{ breaks: true, gfm: true }});
document.getElementById('md').innerHTML = DOMPurify.sanitize(rendered);
function goBack() {{
  // Opened via window.open(..., 'noopener'), so opener is null — just close.
  // If close is blocked (tab opened manually), fall back to history.
  try {{ window.close(); }} catch (e) {{}}
  if (!window.closed) {{ window.history.back(); }}
}}
</script>
</body></html>"""
    return HTMLResponse(content=html)

@app.get("/balance")
async def balance():
    return {"mode": "subscription", "provider": "claude-code"}

# Запуск: uvicorn entrypoints.web_ui:app --host 0.0.0.0 --port 8000
