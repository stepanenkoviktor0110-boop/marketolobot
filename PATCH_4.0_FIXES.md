# Исправления после Patch 4.0

После применения `Patch 4.0.md` нужно сделать следующее через Codex.

---

## Баги для исправления

### 1. `app = FastAPI(...)` объявлен после декораторов — NameError при старте

В файле `entrypoints/web_ui.py` строка `app = FastAPI(title="PMF Pipeline v4.0")` стоит
в секции `=== 6. V3.0+V4.0 ROUTES ===`, ПОСЛЕ `@app.post("/api/chat")`, `@app.get("/api/pmf_score")` и т.д.
Python выполняет декораторы сверху вниз, `app` ещё не существует → `NameError`.

**Фикс:** переместить `app = FastAPI(title="PMF Pipeline v4.0")` в начало файла,
сразу после блока логирования (до первого `@app.` декоратора).

---

### 2. `BackgroundTasks = BackgroundTasks()` — сломанная инжекция

В эндпоинтах `/api/run`, `/api/ctx` параметр объявлен как:
```python
async def queue_task(..., bg: BackgroundTasks = BackgroundTasks()):
```
FastAPI не инжектирует `BackgroundTasks` если у него есть default-значение.
Задачи будут запускаться в никуда.

**Фикс:** убрать `= BackgroundTasks()` — оставить только тип:
```python
async def queue_task(bg: BackgroundTasks, project: str = Form(...), ...):
```

---

### 3. Path traversal в `/download`

```python
path = PROJECTS_ROOT / project / filename.lstrip("/")
```
`lstrip("/")` недостаточно — `../../etc/passwd` пройдёт.

**Фикс:** добавить проверку realpath:
```python
async def download(project: str, filename: str):
    if ".." in project or ".." in filename:
        raise HTTPException(400, "Invalid path")
    path = (PROJECTS_ROOT / project / filename.lstrip("/")).resolve()
    if not str(path).startswith(str(PROJECTS_ROOT.resolve())):
        raise HTTPException(400, "Invalid path")
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, filename=path.name)
```

---

### 4. Токен-поле снова в UI — убрать, инжектировать с сервера

Patch 4.0 вернул поле ввода токена. Нужно:

1. В функции `dashboard()` убрать блок:
   ```html
   <div style="display:flex;...">
     <span>🔑 Token:</span><input type="password" id="apiToken" ...>
     <button onclick="saveToken()">💾</button>
   </div>
   ```

2. В JS убрать: `const tokenKey`, `let token = localStorage...`, `$('apiToken').value=token`,
   функцию `saveToken()`, и проверки `if(!token) return alert(...)`.

3. Заменить на инжекцию с сервера (в начале `<script>` блока):
   ```python
   # Python f-string — API_TOKEN — переменная из конфига
   const token = '{API_TOKEN}';
   ```

4. Оставить `const headers = () => ({{ 'Authorization': \`Bearer ${{token}}\` }});` — без изменений.

---

### 5. JS template literals в Python f-string — проверить экранирование

Весь JS внутри f-string должен использовать `${{...}}` вместо `${...}`.
CSS `{}` → `{{}}`.

Примеры которые часто ломаются:
- `` `Bearer ${token}` `` → `` `Bearer ${{token}}` ``
- `` `<div style="width:${x.progress}%` `` → `` `<div style="width:${{x.progress}}%` ``
- `` `<a href="${x.file}">` `` → `` `<a href="${{x.file}}">` ``

---

### 6. `run_stage` — async, нельзя оборачивать в `lambda` без await

В `execute_pipeline`:
```python
# НЕВЕРНО — lambda возвращает coroutine, не запускает его
res = await run_with_retry(lambda: run_stage(stage, input_text, str(project_path)), ...)
```

`run_stage` в `core/router.py` — это `async def`. `run_with_retry` проверяет `asyncio.iscoroutinefunction(func)`,
но `lambda` — не coroutinefunction, даже если возвращает coroutine.

**Фикс:**
```python
from core.router import run_stage
res = await run_with_retry(run_stage, args=(stage, input_text, str(project_path)), max_retries=2)
```

---

## Проверка после всех правок

```bash
cd /home/xander_bot/botz/МаркетБот
.venv/bin/python -c "import entrypoints.web_ui; print('OK')"
sudo systemctl restart pmf-web && sleep 2
curl -s http://127.0.0.1:8080/ | head -3
# Если порт сменился в Patch 4.0 — проверить systemd/pmf-web.service
# pmf-web слушает на 8080 (uvicorn), nginx проксирует на 8090 (внешний)
```

---

## Конфиг — что добавить в `config.yaml`

Patch 4.0 читает секцию `webui:`:
```yaml
webui:
  owner_token: "тот же токен что был web_api_token"
  shared_token: "командный_токен_для_просмотра"
  features:
    ai_chat: true
    pmf_score: true
    activity_feed: true
    voice_input: false
    auto_schedule: {enabled: false, cron: "0 10 * * *"}
```

Если секции нет — `OWNER_TOKEN` возьмётся из переменной окружения `PMF_WEB_TOKEN`
или будет `"change-me-please"` (сервис запустится но API не будет работать).
