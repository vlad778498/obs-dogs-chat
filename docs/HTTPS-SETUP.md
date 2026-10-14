# Настройка HTTPS для «Собак чата»

Python-сервер (`dogs_chat.py`) умеет отдавать только обычный HTTP.
HTTPS мы добавляем **реверс-прокси** — самый правильный и простой способ:
сервер слушает только `127.0.0.1`, а nginx/Caddy принимает шифрованный трафик
на порту 443 и передаёт его внутрь. TLS-сертификат при этом можно получить
бесплатно (Let's Encrypt) или сделать самоподписанный.

Два варианта на выбор:

- **Вариант A — Caddy** (рекомендую): автоматический сертификат Let's Encrypt,
  конфиг из 5 строк. Нужен домен, указывающий A-записью на IP вашего сервера.
- **Вариант B — nginx + самоподписанный сертификат**: без домена, за 2 минуты,
  но браузер/OBS будет ругаться на сертификат (в OBS это отключается одной
  галочкой).

---

## Шаг 1. Сервер должен слушать только localhost

В `config/settings.json` (или `/opt/dogs-chat/config/settings.json`):

```json
{
  "http_host": "127.0.0.1",
  "http_port": 8377
}
```

Это важно: наружу порт 8377 больше не открываем вообще — весь доступ идёт
через 443. После правки:

```bash
sudo systemctl restart dogs-chat
curl -s http://127.0.0.1:8377/api/dogs   # должно отвечать
```

Закройте проброс/правило firewall на порт 8377, если открывали ранее.

---

## Вариант A. Caddy + Let's Encrypt (нужен домен)

Подойдёт любой домен второго уровня или поддомен, например `dogs.moy-domen.ru`.

### A.1. DNS
Укажите A-запись `dogs.moy-domen.ru -> IP вашего сервера` (публичный IP,
не внутренний 10.x!). Порты 80 и 443 должны быть открыты наружу.

Проверка с любой машины: `ping dogs.moy-domen.ru` должен резолвиться в ваш IP.

### A.2. Установка Caddy (в CT Debian 13)

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
    reverse_proxy 127.0.0.1:8377
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
```

### A.5. OBS

Browser Source → URL: `https://dogs.moy-domen.ru`
(Cache можно оставить включённым — сертификат валидный, предупреждений нет.)

> Если проксируется SSE-поток и соединения «зависают», убедитесь, что вы
> используете достаточно свежий Caddy (>= 2.6) — буферизация ответов у него
> для reverse_proxy по умолчанию отключена.

---

## Вариант B. nginx + самоподписанный сертификат (без домена)

### B.1. Установка

```bash
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
    listen 443 ssl;
    http2 on;
    server_name _;

    ssl_certificate     /etc/nginx/ssl/dogs-chat.crt;
    ssl_certificate_key /etc/nginx/ssl/dogs-chat.key;

    location / {
        proxy_pass http://127.0.0.1:8377;
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

# редирект с http на https
server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}
```

Включаем и проверяем:

```bash
sudo ln -sf /etc/nginx/sites-available/dogs-chat /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl enable nginx
```

### B.4. Firewall

Открыть 80 и 443 (и закрыть 8377):

```bash
# внутри CT, если включён nftables/pve-firewall — по вашей схеме;
# пример через ufw:
sudo apt install -y ufw
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw delete allow 8377/tcp || true
```

Не забудьте про firewall Proxmox и облако/роутер провайдера (порты 80/443).

### B.5. Настройка OBS (важно!)

Сертификат самоподписанный — Chromium в OBS откажется грузить страницу,
пока не отключить проверку:

1. Источник «Браузер» → URL: `https://ВАШ_IP`
2. Свойства источника → снимите галку **«Ignore SSL certificate errors»**...
   точнее наоборот: **поставьте** её (в английской версии —
   «Ignore SSL errors when loading page»). В разных версиях OBS галка
   называется чуть иначе, но смысл один — разрешить невалидный сертификат.
3. Обновите источник (Ctrl+R / Deactivate→Activate).

### B.6. Проверка

```bash
curl -k -I https://ВАШ_СЕРВЕР_IP     # -k = не проверять сертификат
```

---

## Как проверить, что HTTPS реально работает

| Симптом | Причина / решение |
|---|---|
| `curl https://...` → connection refused | nginx/caddy не слушает 443: `ss -tlnp \| grep 443` |
| 502 Bad Gateway | сервер не отвечает на 127.0.0.1:8377: `systemctl status dogs-chat` |
| Собаки появляются, но не обновляются | забыли `proxy_buffering off` (nginx) — SSE буферизируется |
| OBS показывает пустую страницу (вариант B) | не включена галка игнора SSL-ошибок в свойствах Browser Source |
| Caddy: `certificate acquire failed` | домен не резолвится на этот IP или порт 80 закрыт |

---

## Безопасность

Порт 443 открыт всему интернету — страница публичная (это только визуализация
чата, никаких секретов), но при желании ограничьте доступ:

- **Basic Auth в nginx** — тогда в URL Browser Source пишите
  `https://логин:пароль@ВАШ_IP` (OBS такое поддерживает):
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
