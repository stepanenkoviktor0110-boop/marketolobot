# Бэклог МаркетБот

## 🔒 HTTPS для pmf-web (голосовые записи в браузере)

**Приоритет:** средний  
**Статус:** в бэклоге

**Проблема:**  
`navigator.mediaDevices.getUserMedia` работает только в secure context (HTTPS или localhost).  
Сейчас pmf-web слушает на `127.0.0.1:8080` без SSL → голосовая запись в браузере недоступна.

**Что нужно сделать:**  
Настроить nginx как HTTPS-реверс-прокси для pmf-web с SSL-сертификатом Let's Encrypt.

**Промт для делегирования:**

```
Настрой HTTPS для FastAPI-сервиса pmf-web на VPS 37.233.82.205 (Ubuntu 24.04, user: xander_bot).

Что уже есть:
- pmf-web запущен через systemd, слушает на 127.0.0.1:8080
- nginx установлен, есть конфиги в /etc/nginx/sites-enabled/
- certbot установлен (проверь: certbot --version)
- домен будет предоставлен (вставь сюда)

Что нужно:
1. Создать nginx-конфиг /etc/nginx/sites-available/pmf-web:
   - server_name <домен>
   - слушать 443 ssl
   - proxy_pass http://127.0.0.1:8080
   - проксировать заголовки: X-Real-IP, X-Forwarded-For, X-Forwarded-Proto
2. Выпустить сертификат: certbot --nginx -d <домен>
3. Добавить редирект HTTP → HTTPS (80 → 443)
4. Проверить: curl -I https://<домен>/ → 200 OK

После настройки голосовая запись в web UI (navigator.mediaDevices.getUserMedia) 
заработает — сейчас она заблокирована браузером из-за отсутствия HTTPS.
```

**Файлы:**
- Сервис: `/etc/systemd/system/pmf-web.service`
- Web UI: `/home/xander_bot/botz/МаркетБот/entrypoints/web_ui.py`
