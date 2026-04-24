# Deployment & Operations

## Платформа
**VPS** Ubuntu 24.04 (xander_bot), 5.8 GB RAM. Без контейнеров, два процесса под systemd.
Доступ: `ssh xander_bot@37.233.82.205`. Деплой кодовой базы — git pull в репозиторий, редактирование на месте.

## Сервисы systemd

| Юнит | Что делает | Перезапуск | Логи |
|------|------------|------------|------|
| `pmf-web.service` | Web UI (FastAPI uvicorn workers=2 на 127.0.0.1:8080) | `sudo systemctl restart pmf-web` | `sudo journalctl -u pmf-web -n 50` |
| `marketbot.service` | Telegram-бот (aiogram polling) | `sudo systemctl restart marketbot` | `sudo journalctl -u marketbot -n 50` |

После любой правки в `core/`, `entrypoints/`, `config.yaml` нужен restart соответствующего юнита.

Файлы юнитов: `systemd/` в корне проекта (для справки, рабочие копии в `/etc/systemd/system/`).

## Конфигурация

`config.yaml` в корне (в `.gitignore`, содержит секреты). Шаблон без секретов: `config.example.yaml`.

Ключевые секции:
- `bot.token` — Telegram Bot API токен (BotFather)
- `owner_id` — Telegram user ID владельца
- `llm.claude` — настройки Claude Code CLI (`bin`, `timeout`, `draft_model`, `polish_model`)
- `routing.{stage}` — модели per-stage (`haiku`/`sonnet`/`null`)
- `webui.owner_token` / `webui.shared_token` — токены доступа к Web UI

Подробнее по разделам — см. tech-stack.md.

## Переменные окружения

Опциональные overrides (если заданы, имеют приоритет над `config.yaml`):
- `CLAUDE_BIN` — путь к `claude` CLI (default: `/home/xander_bot/.npm-global/bin/claude` или `shutil.which("claude")`)
- `CLAUDE_CWD` — рабочая директория для subprocess claude (default: `/tmp` чтобы не подгружать project CLAUDE.md)
- `PMF_WEB_TOKEN` — fallback owner token, если в `config.yaml` нет `webui.owner_token`

## Ротация webui-токенов

Токен больше НЕ зашит в HTML: Web UI использует cookie-session (HttpOnly `pmf_auth`, 30 дней). Ротация: правка `webui.owner_token` в config.yaml → restart → старые cookie станут невалидными → на всех открытых вкладках JS-обёртка `fetch` автоматически редиректит на `/login` при 401. Пользователь вводит новый токен один раз.

**Процедура (запускать в shell владельца, не давать агенту читать/писать config.yaml):**

1. Сгенерировать новый токен и подменить старое значение в `webui.owner_token` и `web_api_token`:
   ```
   python3 -c 'import secrets,pathlib,os,re; new=secrets.token_hex(32); p=pathlib.Path("config.yaml"); t=p.read_text("utf-8"); old=re.search(r"owner_token:\s*\"([^\"]+)\"",t).group(1); p.write_text(t.replace(old,new),"utf-8"); pathlib.Path(".new_token.txt").write_text(new,"utf-8"); os.chmod(".new_token.txt",0o600); print("OK")'
   ```
2. `sudo systemctl restart pmf-web.service`
3. Проверить: `curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $(cat .new_token.txt)" http://127.0.0.1:8080/api/balance` → `200`
4. Обновить токен везде, где он сохранён вручную (закладки/curl-скрипты/Telegram-заметки)
5. Открытые вкладки дашборда — `Ctrl+R`

`.new_token.txt` хранится в `.gitignore` (chmod 600), используется как буфер для сверки.

## Логи и мониторинг

| Источник | Где смотреть |
|----------|---------------|
| pmf-web stdout/stderr | `sudo journalctl -u pmf-web -f` |
| marketbot stdout/stderr | `sudo journalctl -u marketbot -f` |
| FastAPI access log | внутри pmf-web journal (формат uvicorn) |
| Лог действий Web UI | `data/activity.json` (последние 100 событий) |
| Лог трат токенов LLM | `projects/{project}/spend.json` (per-project, append-only) |
| Гостевая активность | `data/guest_activity.json` |
| Heartbeat бота | `data/bot.heartbeat` (mtime обновляется каждые 30 мин) |

Health-check эндпоинта нет. Проверка живости — `GET /` (Web UI) и watchdog через `data/bot.pid` + heartbeat (см. architecture.md → Watchdog).

## Откат

Git: `git log --oneline` → выбрать предыдущий коммит → `git checkout <sha>` → restart соответствующего юнита.

Конфиг: `config.yaml` не в git. Резервируй сам перед правкой (`cp config.yaml config.yaml.bak`).

Данные проектов: `projects/{name}/` — файловая система, бэкап на усмотрение (rsync/snapshot).

## Pre-deploy checklist

Полностью ручной деплой, CI/CD нет.
- [ ] `git pull` в репозитории
- [ ] Если менялся `requirements.txt`: `.venv/bin/pip install -r requirements.txt`
- [ ] Если менялся `core/` или `entrypoints/`: restart соответствующего юнита
- [ ] Если менялся `config.yaml`: restart обоих юнитов
- [ ] Smoke-тест: `curl http://127.0.0.1:8080/` (200) + `/api/balance` с токеном (200)
