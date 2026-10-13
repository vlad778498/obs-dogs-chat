# Запуск «Собак чата» на Proxmox CT (Debian 13)

Пошаговая инструкция: создаём контейнер (LXC) с Debian 13, ставим сервис,
настраиваем доступ из OBS.

---

## Шаг 0. Предположения

- У вас есть работающий Proxmox VE, хост видит интернет.
- Действия на хосте Proxmox выполняются от root (`pvesh`, `pct` и т.д.).
- Дальше по тексту:
  - `CTID` — номер контейнера, например `210`;
  - `10.0.0.50` — IP контейнера (подставьте свой);
  - канал Twitch — `мой_канал` (замените).

---

## Шаг 1. Создать контейнер с Debian 13

На хосте Proxmox:

```bash
# 1.1 Скачать шаблон Debian 13 (trixie), если ещё нет
pveam update
pveam available | grep debian-13        # найдите имя, напр. debian-13-standard_13.x-1_amd64.tar.zst
pveam download local debian-13-standard_13.2-1_amd64.tar.zst

# 1.2 Создать CT из шаблона
pct create 210 local:vztmpl/debian-13-standard_13.2-1_amd64.tar.zst \
  --hostname dogs-chat \
  --memory 512 --swap 512 \
  --core 1 \
  --disk 2G \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 \
  --onboot 1

# 1.3 Запустить и войти
pct start 210
pct enter 210
```

Замечания:
- Ресурсов нужно совсем мало: Python + websocket + HTTP. 512 МБ RAM хватит с запасом.
- `ip=dhcp` — проще всего. Если нужен статический IP:
  `--net0 name=eth0,bridge=vmbr0,ip=10.0.0.50/24,gw=10.0.0.1`
- В unprivileged-контейнере systemd работает «из коробки», ничего дополнительно включать не нужно.

Проверьте внутри CT сеть:

```bash
ping -c 2 deb.debian.org
```

---

## Шаг 2. Установить пакеты (внутри CT)

```bash
apt update && apt upgrade -y
apt install -y git python3 python3-pip python3-venv ca-certificates adduser curl
```

Debian 13 не позволяет ставить pip-пакеты в системный Python (PEP 668,
`externally-managed-environment`), поэтому используем venv — это правильный путь.

---

## Шаг 3. Получить код проекта

```bash
mkdir -p /opt && cd /opt
git clone https://github.com/vlad778498/obs-dogs-chat.git dogs-chat
cd dogs-chat
```

Если репозиторий приватный — склонируйте через токен:

```bash
git clone https://<TOKEN>@github.com/vlad778498/obs-dogs-chat.git dogs-chat
```

(или скачайте zip: `curl -L -o app.zip https://github.com/vlad778498/obs-dogs-chat/archive/refs/heads/master.zip && apt install -y unzip && unzip app.zip && mv obs-dogs-chat-master dogs-chat`)

---

## Шаг 4. Виртуальное окружение и зависимости

```bash
python3 -m venv /opt/dogs-chat/venv
/opt/dogs-chat/venv/bin/pip install --upgrade pip
/opt/dogs-chat/venv/bin/pip install websockets
```

Проверка:

```bash
/opt/dogs-chat/venv/bin/python -c "import websockets; print(websockets.__version__)"
```

---

## Шаг 5. Пользователь-сервис и права

```bash
adduser --system --group --home /opt/dogs-chat --shell /usr/sbin/nologin dogssvc
chown -R dogssvc:dogssvc /opt/dogs-chat
```

---

## Шаг 6. Конфигурация

В репозитории лежит шаблон настроек:

```bash
cp config/settings.example.json config/settings.json
nano config/settings.json
```

Итоговый файл должен выглядеть так:

```json
{
  "channels": ["мой_канал"],
  "active_window_seconds": 3600,
  "max_dogs": 20,
  "http_port": 8377,
  "http_host": "0.0.0.0"
}
```

Ключевые моменты:
- `"channels"` — логи Twitch-каналов **строчными буквами**, без `#`.
- `"http_host": "0.0.0.0"` — обязательно, иначе OBS с другой машины не достучится.
- `"active_window_seconds": 3600` — окно активности (час, как вы и хотели).
- `"max_dogs"` — максимум собак на экране.

---

## Шаг 7. Systemd-юнит

Создаём юнит под venv:

```bash
cat > /etc/systemd/system/dogs-chat.service <<'EOF'
[Unit]
Description=OBS Dogs of Chat (Twitch active chatters as dogs)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=dogssvc
Group=dogssvc
WorkingDirectory=/opt/dogs-chat
ExecStart=/opt/dogs-chat/venv/bin/python /opt/dogs-chat/dogs_chat.py
Restart=always
RestartSec=5

# немного изоляции
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now dogs-chat
```

Проверка статуса и логов:

```bash
systemctl status dogs-chat --no-pager
journalctl -u dogs-chat -f      # Ctrl+C для выхода
```

В логах должны появиться строки вида `Connected to irc-ws.chat.twitch.tv` и
`HTTP server on 0.0.0.0:8377`.

---

## Шаг 8. Быстрая проверка на самом CT

```bash
curl -s http://localhost:8377/api/dogs     # должен вернуть JSON {"dogs": [...]}
curl -sI http://localhost:8377/            # 200 OK, text/html
```

Если в логах ошибка подключения к Twitch — проверьте DNS и исходящий трафик:

```bash
getent hosts irc-ws.chat.twitch.tv
```

---

## Шаг 9. Сетевой доступ (уровни firewall)

Чтобы OBS на вашем ПК видел страницу, порт 8377 должен быть открыт на всех уровнях:

**9.1 Внутри CT.** Debian 13 по умолчанию использует nftables; ufw обычно выключен.
Проверьте: `systemctl is-active nftables ufw`. Если ufw активен:

```bash
ufw allow 8377/tcp
```

**9.2 Firewall Proxmox**, если включён (Datacenter → Firewall или CT → Firewall):
добавьте правило allow TCP 8377 для IP контейнера. На CT firewall включается
галкой опции «Firewall» у самого контейнера.

**9.3 Внешний firewall/облако.** Если Proxmox стоит в облаке (Hetzner Cloud,
Selectel, VK Cloud и т.п.) — откройте TCP 8377 в security group / firewall
панели провайдера. Если Proxmox дома за роутером — пробросьте порт 8377 на IP
контейнера либо используйте Overlay-сеть.

Проверка с вашего ПК (не с CT!):

```bash
curl -s http://10.0.0.50:8377/api/dogs
```

---

## Шаг 10. Подключение в OBS

1. Откройте OBS на своём ПК.
2. Источник (Sources) → **+** → **Browser** (Браузер).
3. Параметры:
   - URL: `http://10.0.0.50:8377` (IP вашего CT)
   - Width: `1920`, Height: `1080` (или размер области под собак)
   - ✅ «Refresh cache when page becomes visible» — можно оставить
   - FPS Customs: можно включить 30 для плавности анимаций
4. Собаки должны появиться в течение нескольких секунд после сообщений в чате.

Совет: для настройки размера/расположения источника удобно использовать
демо-режим (Шаг 11) — он эмулирует чат сам, без реального стрима.

---

## Шаг 11. Демо-режим (опционально, для настройки сцены)

Остановить боевой сервис и временно запустить демо:

```bash
systemctl stop dogs-chat
sudo -u dogssvc /opt/dogs-chat/venv/bin/python /opt/dogs-chat/dogs_chat.py --demo
# в OBS URL тот же: http://10.0.0.50:8377
# Ctrl+C, затем systemctl start dogs-chat — вернуться к реальному чату
```

---

## Шаг 12. Безопасность (настоятельно рекомендуется)

Сервис отдаёт страницу по HTTP без авторизации. Кто узнает IP:порт — сможет
смотреть ваших чатеров и занимать SSE-соединения. Варианты защиты:

**Вариант А. Разрешить доступ только со своего IP (проще всего).**
Внутри CT (nftables):

```bash
apt install -y nftables
cat > /etc/nftables.conf <<'EOF'
table inet dogs {
  chain in {
    type filter hook input priority 0; policy accept;
    tcp dport 8377 ip saddr { 203.0.113.10, 10.0.0.0/24 } accept
    tcp dport 8377 counter drop
  }
}
EOF
systemctl enable --now nftables
```
(подставьте белый IP вашего ПК/сети вместо 203.0.113.10)

**Вариант Б. Nginx-реверс с Basic Auth + HTTPS.** Ставите nginx на CT,
он слушает 443, проксирует `/` и `/api` на localhost:8377 и требует
`auth_basic`. В OBS тогда URL `https://...` (при самоподписанном сертификате
в Browser Source ничего специального не нужно — CEF предупредит, но работать будет
со снятой проверкой). Схема стандартная, могу расписать отдельно.

**Вариант В.** Не светить порт наружу вообще: поднять WireGuard/Tailscale между
ПК и CT, использовать внутренний туннельный IP. Самый надёжный вариант, если
OBS всегда на одном вашем устройстве.

---

## Полезные команды

```bash
systemctl restart dogs-chat          # применить settings.json после правки
journalctl -u dogs-chat -n 100       # последние 100 строк лога
pct enter 210                        # зайти в контейнер с хоста Proxmox
pct exec 210 -- systemctl status dogs-chat   # то же одной командой с хоста
```

Обновление кода:

```bash
cd /opt/dogs-chat && git pull && systemctl restart dogs-chat
```

---

## Частые проблемы

| Симптом | Причина / решение |
|---|---|
| `status=203/EXEC` у сервиса | Неверный путь в ExecStart. Должно быть `/opt/dogs-chat/venv/bin/python`. |
| Логи: `ModuleNotFoundError: websockets` | Зависимость поставлена в системный python, а не в venv. Повторите Шаг 4. |
| Логи: ошибка DNS/подключения к IRC | Нет доступа в интернет из CT. Проверьте bridge (vmbr0), `/etc/resolv.conf` (должен быть рабочий nameserver). |
| `Address already in use` | Порт 8377 занят другим процессом: `ss -ltnp \| grep 8377`, поменяйте `http_port`. |
| OBS показывает пустую серую область | Порт закрыт на каком-то из firewalls (Шаг 9) или `http_host` остался `127.0.0.1`. |
| Собаки не появляются, но соединение есть | Лог канала в `channels` написан с заглавной буквы или с `#` — нужен чистый lowercase-логин. Стрим должен быть онлайн (Twitch IRC отдаёт сообщения только живого канала). |
| После ребута Proxmox сервис не поднялся | Нужно и `--onboot 1` при создании CT, и `systemctl enable dogs-chat` — проверьте оба. |
