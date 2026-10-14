# Настройка HTTPS для «Собак чата»

Python-сервер (`dogs_chat.py`) умеет отдавать только обычный HTTP.
HTTPS мы добавляем **реверс-прокси** — самый правильный и простой способ:
nginx/Caddy принимает шифрованный трафик и передаёт его внутрь на локальный
порт 8377. TLS-сертификат при этом можно получить бесплатно (Let's Encrypt)
или сделать самоподписанный.

⚠️ **На этом IP/CT уже живут другие сервисы**, поэтому правило такое:

- Собаки остаются доступны по **порту 8377** (как сейчас) — ничего не ломаем.
- HTTPS навешиваем на **отдельный нестандартный порт** (по умолчанию **8443**),
  чтобы НЕ занимать 443/80 — они принадлежат другим сервисам.
  URL в OBS станет: `https://ВАШ_IP:8443`.

Два варианта на выбор:

- **Вариант A — Caddy** (рекомендую): автоматический сертификат Let's Encrypt,
  конфиг из 5 строк. Нужен домен, указывающий A-записью на IP вашего сервера
  (для домена Let's Encrypt порт может быть любой — проверка проходит по
  HTTP-01, поэтому Caddy слушающий только 8443 сертификата LE не получит;
  вариант A подходит, если домен есть и вы готовы выделить ему порт 443 через
  уже работающий веб-сервер, либо использовать DNS-челлендж).
- **Вариант B — nginx + самоподписанный сертификат на порту 8443**: без домена,
  за 2 минуты, не трогает порты других сервисов, но браузер/OBS будет ругаться
  на сертификат (в OBS это отключается одной галочкой).

Если у вас уже есть nginx с other-сервисами — просто добавьте туда ещё один
`server`-блок на 8443 (Вариант B), второй nginx ставить не нужно.

---

## Шаг 1. Сервер остаётся на 8377, слушает localhost

В `config/settings.json` (или `/opt/dogs-chat/config/settings.json`):

```json
{
  "http_host": "127.0.0.1",
  "http_port": 8377
}
```

Наружу на 8377 пускаем напрямую (как и раньше), а реверс-прокси ходит внутрь
на тот же `127.0.0.1:8377`. Один и тот же бэкенд доступен двумя путями:
`http://IP:8377` и `https://IP:8443`. После правки:

```bash
sudo systemctl restart dogs-chat
curl -s http://127.0.0.1:8377/api/dogs   # должно отвечать
```

Правило firewall на порт 8377 оставляем открытым (проброс не закрываем),
плюс открываем 8443.

---

## Вариант A. Caddy + Let's Encrypt (нужен домен)

Подойдёт любой домен второго уровня или поддомен, например `dogs.moy-domen.ru`.

⚠️ Вариант A предполагает, что **порты 80/443 на этом IP свободны** для данного
домена (Caddy должен занимать 443, иначе Let's Encrypt HTTP-01 не пройдёт).
Если 443 занят другим сервисом — используйте **Вариант B** (порт 8443), либо
оставьте основной веб-сервер на 443 и добавьте собак как `location` в его
конфиг с TLS из уже имеющегося сертификата.

### A.1. DNS
Укажите A-запись `dogs.moy-domen.ru -> IP вашего сервера` (публичный IP,
не внутренний 10.x!). Порты 80 и 443 должны быть открыты наружу.

Проверка с любой машины: `ping dogs.moy-domen.ru` должен резолвиться в ваш IP.

### A.2. Установка Caddy (в CT Debian 13)

Если nginx/Caddy другого сервиса уже слушает 80/443 — второй Caddy ставить
нельзя (порты конфликтуют). Тогда пропускайте вариант A или используйте
Caddy на нестандартном порту с самоподписанным/внешним сертификатом.

```bash
sudo apt install -y curl gnupg
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://dl.caddyserver.com/deb-repo.gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/caddy.gpg
echo "deb [signed-by=/etc/apt/keyrings/caddy.gpg] https://dl.caddyserver.com/deb-repo stable main" | \
  sudo tee /etc/apt/sources.list.d/caddy.list
sudo apt update && sudo apt install -y caddy
```

### A.3. Конфиг

Файл `/etc/caddy/Caddyfile`:

```caddy
dogs.moy-domen.ru {
    reverse_proxy 127.0.0.1:8377   # порт 8377 остаётся и для прямого http://IP:8377
}
```

Caddy сам запросит и будет сам продлевать сертификат Let's Encrypt.

```bash
sudo systemctl reload caddy
sudo systemctl enable caddy
```

Логи: `journalctl -u caddy -f`. Первый запрос сертификата занимает ~10 секунд.

### A.4. Проверка

```bash
curl -I https://dogs.moy-domen.ru          # HTTP/2 200, без ошибок TLS
curl -s  http://ВАШ_IP:8377/api/dogs       # прямой доступ по 8377 жив
```

### A.5. OBS

Browser Source → URL: `https://dogs.moy-domen.ru`
(Cache можно оставить включённым — сертификат валидный, предупреждений нет.)
Старый `http://ВАШ_IP:8377` тоже продолжает работать.

---

## Вариант B. nginx + самоподписанный сертификат (без домена, порт 8443)

### B.1. Установка

```bash
# если nginx уже стоит (другие сервисы) — ставить ничего не нужно,
# просто переходите к B.2
sudo apt install -y nginx openssl
```

### B.2. Самоподписанный сертификат (срок 10 лет, CN можно любой)

```bash
sudo mkdir -p /etc/nginx/ssl
sudo openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
  -keyout /etc/nginx/ssl/dogs-chat.key \
  -out /etc/nginx/ssl/dogs-chat.crt \
  -subj "/CN=dogs-chat.local" \
  -addext "subjectAltName=IP:ВАШ_СЕРВЕР_IP,DNS:localhost"
sudo chmod 600 /etc/nginx/ssl/dogs-chat.key
```

(если у OBS-машины статический IP — добавьте его в subjectAltName тоже)

### B.3. Конфиг nginx

Файл `/etc/nginx/sites-available/dogs-chat`:

```nginx
server {
    listen 8443 ssl;        # нестандартный порт: 443 занят другими сервисами
    http2 on;
    server_name _;

    ssl_certificate     /etc/nginx/ssl/dogs-chat.crt;
    ssl_certificate_key /etc/nginx/ssl/dogs-chat.key;

    location / {
        proxy_pass http://127.0.0.1:8377;   # тот же бэкенд, что и на :8377
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;

        # обязательно для SSE (стрим обновлений собак):
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 24h;
        chunked_transfer_encoding on;
    }
}
```

Включаем и проверяем (конфиг добавляется к существующему nginx — другие
сайты/сервисы на 80/443 не трогаем; `default` не удаляем, если он обслуживает
что-то ещё):

```bash
sudo ln -sf /etc/nginx/sites-available/dogs-chat /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl enable nginx
```

Если у вас уже есть свой `server`-блок с сертификатом (например, для другого
сервиса), можно вместо этого добавить в него `location /dogs/` с
`proxy_pass http://127.0.0.1:8377/;` — тогда адрес будет
`https://ВАШ_IP/dogs/`. Учтите: UI использует относительные пути (`/api/...`,
`/ui/...`), поэтому при проксировании через префикс нужна строка
`proxy_pass http://127.0.0.1:8377/;` со слэшем и переписыванием пути — проще
всего оставить отдельный порт 8443, как выше.

### B.4. Firewall

Открыть **8443** (порт 8377 остаётся открытым как был — прямой доступ нужен):

```bash
# внутри CT, если включён nftables/pve-firewall — по вашей схеме;
# пример через ufw:
sudo apt install -y ufw
sudo ufw allow 8443/tcp
# 8377 НЕ удаляем — оставляем прямой доступ по порту
```

Не забудьте про firewall Proxmox и облако/роутер провайдера (порт 8443).
Порты 80/443 для собак открывать не нужно — они у других сервисов.

### B.5. Настройка OBS (важно!)

Сертификат самоподписанный — Chromium в OBS откажется грузить страницу,
пока не отключить проверку:

1. Источник «Браузер» → URL: `https://ВАШ_IP:8443`
2. Свойства источника → снимите галку **«Ignore SSL certificate errors»**...
   точнее наоборот: **поставьте** её (в английской версии —
   «Ignore SSL errors when loading page»). В разных версиях OBS галка
   называется чуть иначе, но смысл один — разрешить невалидный сертификат.
3. Обновите источник (Ctrl+R / Deactivate→Activate).

Старый URL `http://ВАШ_IP:8377` продолжает работать — можно переключиться
обратно в любой момент.

### B.6. Проверка

```bash
curl -k -I https://ВАШ_СЕРВЕР_IP:8443   # -k = не проверять сертификат
curl -s  http://ВАШ_СЕРВЕР_IP:8377/api/dogs  # старый путь тоже жив
```

---

## Как проверить, что HTTPS реально работает

| Симптом | Причина / решение |
|---|---|
| `curl https://IP:8443` → connection refused | nginx не слушает 8443: `ss -tlnp \| grep 8443`; проверьте `nginx -t` и что конфиг симлинкнут в sites-enabled |
| Ошибка при старте/релоаде nginx: `bind() to 0.0.0.0:8443 failed (Address already in use)` | порт 8443 занят другим сервисом — выберите другой порт (например 8543) и замените его везде в конфиге, OBS-URL и firewall |
| 502 Bad Gateway | сервер не отвечает на 127.0.0.1:8377: `systemctl status dogs-chat` |
| Собаки появляются, но не обновляются | забыли `proxy_buffering off` (nginx) — SSE буферизируется |
| OBS показывает пустую страницу (вариант B) | не включена галка игнора SSL-ошибок в свойствах Browser Source |
| Caddy: `certificate acquire failed` | домен не резолвится на этот IP или порт 80 закрыт/занят другим веб-сервером |
| Перестали работать другие сервисы после настройки | вы случайно заняли их порт или удалили чужой конфиг (`default`) — верните как было; конфиг собак изолирован в `dogs-chat` и должен висеть только на 8443 |

---

## Безопасность

Порт 8443 открыт всему интернету — страница публичная (это только визуализация
чата, никаких секретов), но при желании ограничьте доступ:

- **Basic Auth в nginx** — тогда в URL Browser Source пишите
  `https://логин:пароль@ВАШ_IP:8443` (OBS такое поддерживает):
  ```nginx
  location / {
      auth_basic "dogs";
      auth_basic_user_file /etc/nginx/.htpasswd;
      ...
  }
  ```
  Файл: `printf 'obs:$(openssl passwd -apr1 ВАШПАРОЛЬ)\n' | sudo tee /etc/nginx/.htpasswd`
- **Caddy**: директива `basic_auth / obs <хеш>` (генерируется `caddy hash-password`).
- Или пускайте OBS только из своей сети (wireguard/tailscale) — тогда HTTPS
  вообще опционален.
