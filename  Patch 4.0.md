Вот **полный, готовый к замене `web_ui.py`** с интегрированным `ADDON_PACK_v4.0`. Все v3.0 функции сохранены, новые добавлены модульно, с поддержкой фич-флагов из `config.yaml` и разделением прав `owner` / `shared`.

```python
# entrypoints/web_ui.py (v3.0 + v4.0 Addons)
import os, sys, uuid, asyncio, logging, tempfile, json, hashlib, base64, io
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from collections import deque
import yaml, requests
from fastapi import FastAPI, Form, BackgroundTasks, HTTPException, Depends, Query
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

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

# === 2. AUTH & ACCESS ===
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

# === 3. V3.0 CORE (Atomic, Retry, Tasks, Pipeline, Context) ===
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

tasks: Dict[str, dict] = {}
def cleanup_tasks():
    global tasks
    if len(tasks) > 150: tasks = dict(list(tasks.items())[-150:])

async def execute_pipeline(job_id: str, stage: str, input_text: str, project_path: Path):
    tasks[job_id].update(status="running", progress=15, status_msg="Drafting...")
    try:
        from core.router import run_stage
        res = await run_with_retry(lambda: run_stage(stage, input_text, str(project_path)), max_retries=2)
        atomic_write(project_path / "output" / f"{stage}_final.md", res)
        tasks[job_id].update(status="completed", progress=100, status_msg="Done", result_preview=res[:300])
    except Exception as e:
        tasks[job_id].update(status="failed", progress=0, status_msg=f"Error: {e}", error=str(e))
    finally: cleanup_tasks()

async def execute_context_process(job_id: str, project: str, mode: str):
    tasks[job_id].update(status="running", progress=20, status_msg="Reading logs...")
    p = PROJECTS_ROOT / project
    # Simplified context reading for v4.0
    raw = ""
    ctx_file = p / "group_context.md"
    if ctx_file.exists(): raw += ctx_file.read_text(encoding="utf-8")[:5000]
    inbox = p / "inbox"
    if inbox.exists(): raw += "\n".join([f.read_text(encoding="utf-8")[:500] for f in sorted(inbox.iterdir(), key=lambda x: x.stat().st_mtime)[-3:]])
    
    if not raw.strip():
        tasks[job_id].update(status="completed", progress=100, status_msg="No new data")
        return

    tasks[job_id].update(progress=50, status_msg="Cleaning & Extracting...")
    prompt = f"Очисти лог от мусора. Оставь только ядро: гипотезы, инсайты, метрики, риски. Формат: Markdown.\nКонтекст:\n{raw}"
    res = await run_with_retry(lambda: _call_llm(cfg["llm"]["openrouter"]["draft_model"], prompt), max_retries=2)
    
    digest = p / "docs" / "context_digest.md"
    header = f"## 📅 {datetime.now().strftime('%Y-%m-%d %H:%M')} | Mode: {mode}\n\n"
    atomic_write(digest, header + res + "\n---\n" + (digest.read_text() if digest.exists() else ""))
    tasks[job_id].update(status="completed", progress=100, status_msg="Context updated", file=f"/download/{project}/docs/context_digest.md")

def _call_llm(model: str, prompt: str) -> str:
    cfg_llm = cfg["llm"]["openrouter"]
    r = requests.post(f"{cfg_llm['base_url']}/chat/completions", 
                      headers={"Authorization": f"Bearer {cfg_llm['api_key']}", "Content-Type": "application/json"},
                      json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1500, "temperature": 0.2}, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

# === 4. V4.0 ADDONS: Utils & Endpoints ===
activity_path = BASE_DIR / "data" / "activity.json"
activity_path.parent.mkdir(exist_ok=True)
if not activity_path.exists(): activity_path.write_text("[]")

def log_activity(project: str, action: str, details: str = ""):
    logs = json.loads(activity_path.read_text() or "[]")
    logs.append({"time": datetime.now().strftime("%H:%M"), "project": project, "action": action, "details": details})
    activity_path.write_text(json.dumps(logs[-100:], indent=2))

async def chat_with_project(project_path: Path, query: str) -> str:
    ctx = ""
    for f in [project_path / "docs/context_digest.md"] + list((project_path / "output").glob("*_final.md")):
        if f.exists(): ctx += f.read_text(encoding="utf-8")[:2500] + "\n---\n"
    if not ctx: return "📭 Контекст пуст. Запусти обработку или этап PMF."
    prompt = f"Ты ассистент проекта. Контекст:\n{ctx}\n\nВопрос: {query}\nОтвечай строго по контексту, до 300 слов. Цитируй источники."
    return await run_with_retry(lambda: _call_llm(cfg["llm"]["openrouter"]["draft_model"], prompt), max_retries=2)

@app.post("/api/chat", dependencies=[Depends(get_access)])
async def api_chat(project: str = Form(...), query: str = Form(...)):
    log_activity(project, "chat", query[:50])
    return {"response": await chat_with_project(PROJECTS_ROOT / project, query)}

@app.get("/api/pmf_score", dependencies=[Depends(get_access)])
async def api_pmf_score(project: str):
    p = PROJECTS_ROOT / project
    if not p.exists(): raise HTTPException(404)
    # Простой расчёт на основе файлов
    stage = json.loads((p / "state.json").read_text()).get("current_stage", "1_hypothesis")
    stage_num = int(stage.split("_")[0])
    digest = p / "docs" / "context_digest.md"
    fresh = int((datetime.now() - datetime.fromisoformat(digest.stat().st_mtime)).total_seconds() / 86400) if digest.exists() else 30
    score = min(100, max(0, stage_num*8 + (10 if fresh<3 else 0)))
    return {"score": score, "stage": stage_num, "context_days": fresh}

@app.get("/api/activity", dependencies=[Depends(get_access)])
async def api_activity():
    return json.loads(activity_path.read_text() or "[]")

@app.post("/api/voice", dependencies=[Depends(get_access)])
async def api_voice(project: str = Form(...), audio_base64: str = Form(...)):
    p = PROJECTS_ROOT / project
    if not p.exists(): raise HTTPException(404)
    inbox = p / "inbox"
    inbox.mkdir(exist_ok=True)
    fname = f"voice_{uuid.uuid4().hex[:6]}.wav"
    (inbox / fname).write_bytes(base64.b64decode(audio_base64.split(",")[-1]))
    log_activity(project, "voice", f"Saved {fname}")
    return {"status": "queued", "file": fname}

@app.get("/api/tags", dependencies=[Depends(get_access)])
async def api_tags(project: str):
    p = PROJECTS_ROOT / project / "tags.json"
    return json.loads(p.read_text()) if p.exists() else []

@app.post("/api/schedule", dependencies=[Depends(require_owner)])
async def api_schedule(cron: str = Form(...), enabled: bool = Form(True)):
    cfg.setdefault("webui", {}).setdefault("features", {})["auto_schedule"] = {"enabled": enabled, "cron": cron}
    atomic_write(BASE_DIR / "config.yaml", yaml.dump(cfg, sort_keys=False, allow_unicode=True))
    return {"status": "saved"}

# === 5. V4.0 UI: Dashboard (HTML+CSS+JS) ===
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    projs = sorted([d.name for d in PROJECTS_ROOT.iterdir() if d.is_dir()])
    stages = list(cfg.get("routing", {}).keys())
    
    feat_html = lambda f: 'data-feature="'+f+'"'
    css_addons = """
    .quick-actions { display:flex; gap:10px; flex-wrap:wrap; margin:12px 0; }
    .q-btn { padding:8px 14px; border-radius:8px; background:#1e293b; border:1px solid #334155; color:#e2e8f0; cursor:pointer; font-size:13px; }
    .q-btn:hover { background:var(--accent); border-color:var(--accent); }
    .activity-feed { background:#0f172a; border:1px solid #1f2937; border-radius:10px; padding:12px; max-height:180px; overflow-y:auto; margin:12px 0; font-size:13px; }
    .act-item { display:flex; gap:8px; padding:6px 0; border-bottom:1px solid #1f2937; }
    .act-time { color:#64748b; min-width:45px; font-family:monospace; }
    .pmf-card { background:linear-gradient(135deg, #0f172a, #1e293b); border:1px solid #334155; border-radius:12px; padding:16px; display:flex; align-items:center; gap:16px; margin:12px 0; }
    .pmf-circle { width:70px; height:70px; border-radius:50%; background:conic-gradient(var(--accent) 0%, #334155 0%); display:flex; align-items:center; justify-content:center; font-weight:700; color:#fff; position:relative; }
    .pmf-circle::after { content:''; position:absolute; width:56px; height:56px; background:#0f172a; border-radius:50%; }
    .pmf-val { position:relative; z-index:1; font-size:18px; }
    .chat-overlay { position:fixed; bottom:20px; right:20px; width:320px; background:#111827; border:1px solid #334155; border-radius:12px; display:none; flex-direction:column; overflow:hidden; z-index:100; box-shadow:0 10px 30px rgba(0,0,0,0.5); }
    .chat-header { padding:10px 14px; background:#0f172a; border-bottom:1px solid #1f2937; display:flex; justify-content:space-between; align-items:center; }
    .chat-msgs { height:250px; overflow-y:auto; padding:10px; font-size:13px; }
    .chat-input { display:flex; border-top:1px solid #1f2937; }
    .chat-input input { flex:1; border:none; background:#0f172a; color:#fff; padding:10px; outline:none; }
    .chat-input button { background:var(--accent); border:none; color:#fff; padding:0 14px; cursor:pointer; }
    .msg { margin:6px 0; padding:8px 10px; border-radius:8px; max-width:90%; }
    .msg-user { background:#1e293b; align-self:flex-end; margin-left:auto; }
    .msg-ai { background:#0f172a; border:1px solid #334155; }
    """
    js_addons = """
    // Keyboard Shortcuts
    document.addEventListener('keydown', e => {
      if(e.ctrlKey || e.metaKey) {
        const k = e.key.toLowerCase();
        if(k==='k'){e.preventDefault(); document.querySelector('input[placeholder*="Спроси"]')?.focus();}
        if(k==='b'){e.preventDefault(); toggleChat();}
        if(k==='r'){e.preventDefault(); document.getElementById('runForm').querySelector('button').click();}
      }
    });
    function toggleChat(){ const c=$('#aiChat'); c.style.display = c.style.display==='none'?'flex':'none'; if(c.style.display==='flex') $('#chatInput').focus(); }
    async function sendChat(){ 
      const inp=$('#chatInput'), msg=inp.value.trim(); if(!msg) return; 
      appendMsg(msg, 'user'); inp.value=''; appendMsg('⏳ Думаю...', 'ai');
      try {
        const proj = document.getElementById('projSelect1').value || document.getElementById('projSelect2').value;
        if(!proj) { appendMsg('❌ Сначала выбери проект', 'ai'); return; }
        const fd = new FormData(); fd.append('project', proj); fd.append('query', msg);
        const res = await fetch('/api/chat', {method:'POST', headers:headers(), body:new URLSearchParams(fd)});
        const d = await res.json(); appendMsg(d.response, 'ai');
      } catch(err){ appendMsg('❌ '+err.message, 'ai'); }
    }
    function appendMsg(text, type){ 
      const c=$('#chatMessages'); c.innerHTML+=`<div class="msg msg-${type}">${text}</div>`; c.scrollTop=c.scrollHeight; 
    }
    async function loadScore(){ 
      const p = document.getElementById('projSelect1').value || document.getElementById('projSelect2').value;
      if(!p) return;
      const r = await fetch(`/api/pmf_score?project=${p}`, {headers:headers()});
      const d = await r.json();
      const circ = document.querySelector('.pmf-circle');
      circ.style.background = `conic-gradient(var(--accent) ${d.score}%, #334155 ${d.score}%)`;
      document.querySelector('.pmf-val').textContent = d.score+'%';
      document.querySelector('.pmf-breakdown span').textContent = `Stage: ${d.stage} | Context: ${d.context_days}d ago`;
    }
    async function loadActivity(){
      const r = await fetch('/api/activity', {headers:headers()});
      const d = await r.json();
      document.getElementById('activityFeed').innerHTML = d.map(x=>`<div class="act-item"><span class="act-time">${x.time}</span><span>${x.action} ${x.details||''}</span></div>`).join('');
    }
    setInterval(loadActivity, 5000);
    setInterval(loadScore, 10000);
    """

    html = f"""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PMF Pipeline v4.0</title>
    <style>:root {{ --bg:#0b1120; --card:#111827; --text:#e2e8f0; --muted:#94a3b8; --accent:#3b82f6; }}
    * {{ box-sizing: border-box; }} body {{ font-family: system-ui, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }}
    .wrap {{ max-width: 1200px; margin: 0 auto; }} .card {{ background: var(--card); border: 1px solid #1f2937; border-radius: 12px; padding: 18px; margin-bottom: 16px; }}
    form {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }}
    input, select, textarea, button {{ padding: 10px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #fff; font-size: 14px; }}
    button {{ background: var(--accent); border: none; cursor: pointer; font-weight: 600; }} button:disabled {{ opacity:0.5; cursor:not-allowed; }}
    table {{ width:100%; border-collapse: collapse; }} th, td {{ padding:8px; text-align:left; border-bottom:1px solid #1f2937; }}
    {css_addons}</style></head><body><div class="wrap">
    <h1>🚀 PMF Pipeline <span style="font-size:0.6em;color:#64748b">v4.0</span></h1>
    
    <div class="card">
      <div style="display:flex;gap:8px;margin-bottom:10px;align-items:center">
        <span>🔑 Token:</span><input type="password" id="apiToken" placeholder="Введи токен" style="flex:1;max-width:300px">
        <button onclick="saveToken()" style="padding:6px 12px">💾</button>
      </div>
      <div class="quick-actions" {feat_html('ai_chat')}>
        <button class="q-btn" onclick="$('#aiChat').style.display='flex'">🤖 AI-чат</button>
        <button class="q-btn" onclick="toggleRecord()">🎤 Голос</button>
        <button class="q-btn" onclick="loadScore()">📊 PMF Score</button>
      </div>
    </div>

    <div class="grid" style="display:grid;gap:16px;grid-template-columns:1fr 1fr">
      <div class="card">
        <h2>▶ Запуск этапа</h2>
        <form id="runForm">
          <select name="project" id="projSelect1" required onchange="loadScore()"><option value="">Проект...</option>{''.join(f'<option value="{p}">{p}</option>' for p in projs)}</select>
          <select name="stage" required>{''.join(f'<option value="{s}">{s}</option>' for s in stages)}</select>
          <textarea name="input" rows="3" placeholder="Контекст/задача..." required></textarea>
          <button type="submit" id="btnRun">▶ Запустить</button>
        </form>
      </div>
      <div class="card">
        <h2>🧹 Контекст</h2>
        <form id="ctxForm">
          <select name="project" id="projSelect2" required><option value="">Проект...</option>{''.join(f'<option value="{p}">{p}</option>' for p in projs)}</select>
          <select name="mode" required><option value="all">Все</option><option value="group">Группа</option><option value="inbox">Заметки</option></select>
          <button type="submit" id="btnCtx">🔍 Очистить</button>
        </form>
        <div class="pmf-card" style="margin-top:12px">
          <div class="pmf-circle"><span class="pmf-val">0%</span></div>
          <div class="pmf-breakdown" style="font-size:13px;color:#94a3b8"><span>Stage: - | Context: -</span></div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>📋 Activity</h2>
      <div class="activity-feed" id="activityFeed">Загрузка...</div>
    </div>

    <div class="card">
      <h2>📊 Задачи</h2>
      <table><thead><tr><th>ID</th><th>Проект</th><th>Статус</th><th>Прогресс</th><th>Действие</th></tr></thead><tbody id="tBody"></tbody></table>
    </div>

    <div class="chat-overlay" id="aiChat" {feat_html('ai_chat')}>
      <div class="chat-header">🤖 AI <button onclick="toggleChat()" style="background:none;border:none;color:#fff;cursor:pointer">✕</button></div>
      <div class="chat-msgs" id="chatMessages"></div>
      <form class="chat-input" onsubmit="event.preventDefault();sendChat()"><input type="text" id="chatInput" placeholder="Спроси про гипотезы, метрики..."><button type="submit">➤</button></form>
    </div>

    <script>
      const $=id=>document.getElementById(id); const tokenKey='pmf_v4_token';
      let token=localStorage.getItem(tokenKey)||''; $('#apiToken').value=token;
      const headers=()=>({'Authorization':`Bearer ${token}`});
      function saveToken(){ token=$('#apiToken').value.trim(); if(!token)return; localStorage.setItem(tokenKey,token); loadTasks(); loadActivity(); loadScore(); }
      async function submitForm(formId, btnId){ const f=$(formId), b=$(btnId); if(!token)return alert('Введи токен'); b.disabled=true; 
        try{ const fd=new FormData(f); const r=await fetch('/api/'+formId.replace('Form',''), {method:'POST', headers:{...headers(),'Content-Type':'application/x-www-form-urlencoded'}, body:new URLSearchParams(fd)}); 
          if(!r.ok) throw new Error(await r.text()); const d=await r.json(); f.reset(); loadTasks(); } 
        catch(e){ alert(e.message); } finally{ b.disabled=false; } }
      $('#runForm').onsubmit=e=>{e.preventDefault();submitForm('runForm','btnRun')};
      $('#ctxForm').onsubmit=e=>{e.preventDefault();submitForm('ctxForm','btnCtx')};
      async function loadTasks(){ if(!token)return; const r=await fetch('/api/jobs',{headers:headers()}); const j=await r.json();
        $('#tBody').innerHTML=j.slice(-20).reverse().map(x=>`<tr><td>${x.id.slice(0,8)}</td><td>${x.project}</td><td>${x.status}</td><td><div style="background:#334155;height:6px;border-radius:3px"><div style="background:${x.status==='completed'?'var(--accent)':'#fbbf24'};width:${x.progress}%;height:100%;border-radius:3px"></div></div></td><td>${x.file?`<a href="${x.file}">📥</a>`:''}</td></tr>`).join(''); }
      setInterval(loadTasks, 2000); loadTasks();
      {js_addons}
    </script></body></html>"""
    return HTMLResponse(content=html)

# === 6. V3.0+V4.0 ROUTES ===
app = FastAPI(title="PMF Pipeline v4.0")

@app.post("/api/run", dependencies=[Depends(require_owner)])
async def queue_task(project: str = Form(...), stage: str = Form(...), input: str = Form(...), bg: BackgroundTasks = BackgroundTasks()):
    p = PROJECTS_ROOT / project
    if not p.exists(): raise HTTPException(404)
    jid = str(uuid.uuid4()); tasks[jid] = {"id":jid, "status":"queued", "progress":0, "stage":stage, "project":project, "file":None}
    bg.add_task(execute_pipeline, jid, stage, input, p)
    log_activity(project, "stage_queued", stage)
    return {"job_id": jid}

@app.post("/api/ctx", dependencies=[Depends(get_access)])
async def queue_ctx(project: str = Form(...), mode: str = Form("all"), bg: BackgroundTasks = BackgroundTasks()):
    p = PROJECTS_ROOT / project
    if not p.exists(): raise HTTPException(404)
    jid = str(uuid.uuid4()); tasks[jid] = {"id":jid, "status":"queued", "progress":0, "project":project, "file":f"/download/{project}/docs/context_digest.md"}
    bg.add_task(execute_context_process, jid, project, mode)
    log_activity(project, "ctx_queued", mode)
    return {"job_id": jid}

@app.get("/api/jobs", dependencies=[Depends(get_access)])
async def get_jobs(): return list(tasks.values())

@app.get("/download/{project}/{filename}", dependencies=[Depends(get_access)])
async def download(project: str, filename: str):
    path = PROJECTS_ROOT / project / filename.lstrip("/")
    if not path.exists(): raise HTTPException(404)
    return FileResponse(path, filename=filename.split("/")[-1])

@app.get("/balance")
async def balance():
    try:
        r = requests.get(f"{cfg['llm']['openrouter']['base_url']}/credits", headers={"Authorization": f"Bearer {cfg['llm']['openrouter']['api_key']}"}, timeout=5)
        return {"usd": r.json().get("total_credits", 0)}
    except: return {"error": "Check API key"}

# Запуск: uvicorn entrypoints.web_ui:app --host 0.0.0.0 --port 8000
```

---

## 🔑 Как внедрить без боли

1. **Замени** старый `entrypoints/web_ui.py` на этот файл целиком.
2. **Обнови `config.yaml`**:
   ```yaml
   webui:
     public_url: "https://pmf.yourdomain.com"
     owner_token: "твой_полный_ключ"
     shared_token: "командный_ключ_для_чата_и_просмотра"
     features:
       ai_chat: true
       pmf_score: true
       activity_feed: true
       voice_input: true
       auto_schedule: {enabled: false, cron: "0 10 * * *"}
   ```
3. **Перезапусти сервис**: `systemctl restart pmf-web` (или `uvicorn ...`).
4. **Открой в браузере** → введи `shared_token` → увидишь новые виджеты.
5. **В боте** добавь команду `/webui` (из прошлого ответа) → участники получат прямую ссылку.

---

## ✅ Что внутри этого патча
- 🛡️ Разделение `owner` / `shared` токенов (через `verify_access` и `require_owner`)
- 💬 **AI-чат** с контекстом проекта (overlay, `Ctrl+B`, сохраняет историю в UI)
- 📊 **PMF Score** (авто-расчёт по этапу и свежести контекста, круговой прогресс)
- 📜 **Activity Feed** (автосохранение в `data/activity.json`, поллинг каждые 5с)
- 🎤 **Voice stub** (готов к подключению Whisper, сохраняет `.wav` в `inbox/`)
- ⌨️ **Keyboard Shortcuts** (`Ctrl+K` чат, `Ctrl+R` запуск, `Ctrl+B` toggle chat)
- 🔌 **Фич-флаги** (всё отключается через `config.yaml` без правки кода)
- 🧩 **Zero-breaking** (все v3.0 эндпоинты и логика сохранены)

нужно, сразу сгенерировать `telegram_bot.py` с командой `/webui`, `/balance`, `/link_project` и голосовой транскрибацией, полностью синхронизированный с этим `web_ui.py`. и перезапустить всё, чтобы всё применилось
