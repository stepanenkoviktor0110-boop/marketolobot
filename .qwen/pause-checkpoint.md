# Pause Checkpoint

**Date:** 2026-04-13 ~16:30
**Feature:** none (ad-hoc bugfix session)
**Commit:** c708489 — "feat: v4.0 - Web UI, RAG, persistence, and critical fixes"

## State

**12 фиксов применено и закоммичено:**
1. «Валеричи» только в группе, в личке — по имени
2. Запрет других языков (китайский фикс)
3. Убраны приветствия в chat-режиме
4. Риски в hypothesis/brainstorm/rate
5. Постобработка голосовых (саммари + действия)
6. Бот признаёт ограничения (detect_unsupported)
7. aiogram 3.27 совместимость
8. Voice дубль в личке — убран 3-й ответ
9. detect_unsupported после detect_intent
10. get_summary() вместо хардкода
11. _pending_intents TTL cleanup
12. .reply → .answer + импорты + asyncio.to_thread

**В процессе: Cloudflare Tunnel для постоянного WebUI домена**
- cloudflared установлен в /tmp/cloudflared
- .env создан с плейсхолдерами CLOUDFLARE_API_TOKEN и CLOUDFLARE_TUNNEL_DOMAIN
- Пользователь ищет правильный раздел в Cloudflare Dashboard
- Временный туннель работает: https://feedback-macro-wife-tulsa.trycloudflare.com

**Uncommitted Changes:** none (working tree clean)
**Running services:**
- marketbot.service ✅
- qwen-agent (autostart.sh) ✅
- wife-bot (2 процесса, без systemd)
- cloudflared tunnel (временный, PID см. в logs)
