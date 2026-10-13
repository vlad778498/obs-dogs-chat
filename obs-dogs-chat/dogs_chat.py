# -*- coding: utf-8 -*-
"""
OBS-плагин "Собаки чата" (Dogs of Chat)

Идея:
  Подключается к чату Twitch выбранных каналов, отслеживает активных
  зрителей (те, кто писал сообщение за последний час) и выводит их
  в виде "собак" на BrowserSource в OBS.

Архитектура:
  [Twitch IRC (WebSocket)] -> [core: учёт активности] -> [HTTP + SSE] -> [ui/index.html (Browser Source)]

Активация плагина в OBS:
  1. Запустить этот скрипт:  python dogs_chat.py
  2. В OBS добавить источник "Браузер" (Browser)
  3. URL:  http://localhost:8377
  4. Размер: 1920x1080 (или под свой макет), фон прозрачный не нужен —
     страница сама рисует фон.

Требования: только стандартная библиотека Python 3.9+ (websockets не нужен —
используется нативный websocket-клиент на сокетах? нет — см. ниже).

Примечание: для подключения к Twitch IRC используется библиотека `websockets`
(ставится одной командой: pip install websockets). Если её нет, плагин умеет
работать в демо-режиме (эмуляция чата), чтобы можно было сразу настроить сцену.
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import websockets.sync.client as ws_client  # websockets >= 12 (sync API)
    HAS_WEBSOCKETS = True
except Exception:
    try:
        import asyncio
        import websockets  # noqa: F401
        HAS_WEBSOCKETS = True  # async API, будет запускаться в отдельном потоке
    except Exception:
        HAS_WEBSOCKETS = False

BASE_DIR = Path(__file__).resolve().parent
UI_DIR = BASE_DIR / "ui"
CONFIG_PATH = BASE_DIR / "config" / "settings.json"

DEFAULT_CONFIG = {
    # Anonymous-подключение к чату Twitch возможно без client id,
    # через wss://irc-ws.chat.twitch.tv:443 (сервер пустого заходит как justinfan*)
    "channels": ["shroud"],            # список каналов чата
    "active_window_seconds": 3600,     # "активен = писал за последний час"
    "refresh_ui_seconds": 5,           # как часто пушить обновление в UI
    "max_dogs": 60,                    # максимум собак на экране
    "nickname": "",                    # ник для IRC (пусто = justinfan random)
    "http_port": 8377,
    "http_host": "127.0.0.1",          # 0.0.0.0 — если сервер на ВМ, а OBS на другой машине
}


# ---------------------------------------------------------------------------
# Учёт активности чатеров
# ---------------------------------------------------------------------------
class ActivityStore:
    """Потокобезопасное хранилище: user -> last_seen_ts."""

    def __init__(self, config: dict):
        self.lock = threading.Lock()
        self.users: "OrderedDict[str, dict]" = OrderedDict()  # login -> {name,color,last}
        self.config = config

    def touch(self, login: str, display_name: str = "", color: str = ""):
        now = time.time()
        with self.lock:
            u = self.users.get(login)
            if u is None:
                u = {"name": display_name or login, "color": color, "last": now, "count": 0}
                self.users[login] = u
            u["last"] = now
            u["count"] += 1
            if display_name:
                u["name"] = display_name
            if color:
                u["color"] = color
            # держим словарь упорядоченным по времени последнего сообщения
            self.users.move_to_end(login)

    def active_users(self) -> list:
        window = self.config.get("active_window_seconds", 3600)
        cutoff = time.time() - window
        with self.lock:
            # prune старых
            stale = [k for k, v in self.users.items() if v["last"] < cutoff]
            for k in stale:
                del self.users[k]
            items = []
            for login, u in reversed(self.users.items()):  # свежие первыми
                items.append({
                    "login": login,
                    "name": u["name"],
                    "color": u["color"],
                    "last_seen": int(u["last"]),
                    "messages": u["count"],
                    "seconds_ago": int(time.time() - u["last"]),
                })
            return items[: self.config.get("max_dogs", 60)]

    def total_active(self) -> int:
        return len(self.active_users())


# ---------------------------------------------------------------------------
# Клиент Twitch IRC (анонимное чтение чата)
# ---------------------------------------------------------------------------
TWITCH_WS_URL = "wss://irc-ws.chat.twitch.tv:443"


def parse_tags(raw_tags: str) -> dict:
    from urllib.parse import unquote
    tags = {}
    if not raw_tags:
        return tags
    for part in raw_tags.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            # Twitch экранирует пробелы/символы backslash-последовательностями
            v = v.replace("\\s", " ").replace("\\:", ";").replace("\\r", "\r").replace("\\n", "\n")
            # некоторые поля (например color) additionally percent-encoded
            if "%" in v:
                v = unquote(v)
            tags[k] = v
    return tags


def parse_irc_line(line: str):
    """Возвращает (nick, tags, command, params) или None."""
    line = line.strip("\r\n")
    if not line:
        return None
    tags = {}
    idx = 0
    if line.startswith("@"):
        sp = line.find(" ")
        tags = parse_tags(line[1:sp])
        idx = sp + 1
    prefix = ""
    rest = line[idx:]
    if rest.startswith(":"):
        sp = rest.find(" ")
        prefix = rest[1:sp]
        rest = rest[sp + 1:]
    nick = prefix.split("!")[0]
    # команда + параметры
    parts = rest.split(" :")
    tokens = parts[0].split(" ")
    cmd = tokens[0] if tokens else ""
    params = tokens[1:]
    trailing = None
    if len(parts) > 1:
        trailing = parts[1]
    return nick, tags, cmd, params, trailing


class TwitchChatThread(threading.Thread):
    def __init__(self, store: ActivityStore, config: dict):
        super().__init__(daemon=True, name="twitch-irc")
        self.store = store
        self.config = config
        self.stop_event = threading.Event()

    def run(self):
        if not HAS_WEBSOCKETS:
            print("[irc] Библиотека 'websockets' не найдена. "
                  "Установите: pip install websockets  (или используйте --demo)")
            return
        channels = [c.strip().lstrip("#").lower() for c in self.config.get("channels", []) if c.strip()]
        if not channels:
            print("[irc] Каналы не заданы — IRC-поток завершён.")
            return
        nick = self.config.get("nickname") or f"justinfan{int(time.time()) % 99999}"
        try:
            self._connect_and_read(nick, channels)
        except Exception as e:
            print(f"[irc] Ошибка соединения: {e}")

    def _connect_and_read(self, nick: str, channels: list):
        # Синхронный API websockets>=12; для старых версий — fallback через asyncio
        conn = None
        try:
            conn = ws_client.connect(TWITCH_WS_URL)
        except AttributeError:
            conn = self._async_connect_fallback()
        if conn is None:
            print("[irc] Не удалось подключиться к Twitch IRC.")
            return
        with conn:
            def send(s):
                conn.send(s.encode() if isinstance(s, str) else s)
            send(f"NICK {nick}")
            send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            for ch in channels:
                send(f"JOIN #{ch}")
            print(f"[irc] Подключено. Каналы: {', '.join(channels)}")
            last_join_check = time.time()
            while not self.stop_event.is_set():
                try:
                    msg = conn.recv(timeout=1.0)
                except TimeoutError:
                    msg = None
                except Exception as e:
                    print(f"[irc] Соединение потеряно: {e}. Переподключение...")
                    break
                if msg is None:
                    if time.time() - last_join_check > 300:
                        for ch in channels:
                            send(f"JOIN #{ch}")
                        last_join_check = time.time()
                    continue
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", "replace")
                for line in msg.split("\r\n"):
                    if line.startswith("PING"):
                        send("PONG :tmi.twitch.tv")
                        continue
                    parsed = parse_irc_line(line)
                    if not parsed:
                        continue
                    nick_user, tags, cmd, params, trailing = parsed
                    if cmd == "PRIVMSG" and nick_user:
                        # игнорируем ботов/сервера, если вдруг
                        login = tags.get("login", nick_user.lower())
                        if login in ("ircv3",):
                            continue
                        self.store.touch(login, tags.get("display-name", nick_user), tags.get("color", ""))
            print("[irc] Поток чата остановлен.")

    def _async_connect_fallback(self):
        """Для старых версий websockets (только async API) — тонкая обёртка."""
        import asyncio
        loop = asyncio.new_event_loop()
        ws = loop.run_until_complete(websockets.connect(TWITCH_WS_URL))

        class Syncish:
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *a):
                loop.run_until_complete(ws.close())
            def send(self_inner, data):
                loop.run_until_complete(ws.send(data))
            def recv(self_inner, timeout=None):
                try:
                    return loop.run_until_complete(asyncio.wait_for(ws.recv(), timeout))
                except asyncio.TimeoutError:
                    return None
        return Syncish()

    def stop(self):
        self.stop_event.set()


# ---------------------------------------------------------------------------
# Демо-режим: эмуляция чата, чтобы настроить вид без Twitch
# ---------------------------------------------------------------------------
DEMO_NAMES = [
    "puglover", "borkmaster", "shiba_inu", "woofmeister", "goodboy_2001",
    "tailwagger", "snaccattack", "zoomies_king", "doggo_nero", "labrador_lisa",
    "husky_drama", "corgi_butt", "beagle_ruff", "mastiff_max", "terrier_tina",
    "retriever_rio", "spaniel_sam", "bulldog_boris", "shepherd_sha", "mutley99",
]


class DemoChatThread(threading.Thread):
    def __init__(self, store: ActivityStore):
        super().__init__(daemon=True, name="demo-chat")
        self.store = store
        self.stop_event = threading.Event()

    def run(self):
        import random
        print("[demo] Эмуляция чата запущена (--demo).")
        # сначала "населить" историю разными давностями
        for n in DEMO_NAMES:
            offset = random.randint(0, 7200)  # 0..2 часа назад
            self.store._seed(n, time.time() - offset)
        while not self.stop_event.is_set():
            name = random.choice(DEMO_NAMES)
            self.store.touch(name.lower(), name)
            time.sleep(random.uniform(0.5, 3.0))

    def stop(self):
        self.stop_event.set()


# добавим метод seed в ActivityStore
def _seed(self, login, ts):
    with self.lock:
        self.users[login] = {"name": login, "color": "", "last": ts, "count": 1}
ActivityStore._seed = _seed


# ---------------------------------------------------------------------------
# HTTP-сервер: отдаёт HTML и JSON/SSE для Browser Source
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    store: ActivityStore = None      # внедряется ниже
    config: dict = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # тишина

    def _send(self, code, ctype, body: bytes, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/stream":
            # SSE — толкает обновления UI без опроса.
            # Отдельно от _send: тело пишется бесконечным потоком без Content-Length.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.close_connection = True  # keep-alive для SSE не нужен
            self.end_headers()
            try:
                interval = max(1, int(self.config.get("refresh_ui_seconds", 5)))
                window = self.config.get("active_window_seconds", 3600)
                while True:
                    dogs = self.store.active_users()
                    data = {
                        "generated_at": int(time.time()),
                        "active_window_seconds": window,
                        "total_active": len(dogs),
                        "dogs": dogs,
                    }
                    payload = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                    self.wfile.write(payload.encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(interval)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        if path == "/" or path == "/index.html":
            f = UI_DIR / "index.html"
            if f.exists():
                self._send(200, "text/html; charset=utf-8", f.read_bytes())
            else:
                self._send(404, "text/plain", b"ui/index.html not found")
        elif path == "/static/dogs.css":
            f = UI_DIR / "dogs.css"
            self._send(200, "text/css; charset=utf-8", f.read_bytes() if f.exists() else b"")
        elif path == "/static/dogs.js":
            f = UI_DIR / "dogs.js"
            self._send(200, "application/javascript; charset=utf-8", f.read_bytes() if f.exists() else b"")
        elif path == "/api/dogs":
            data = {
                "generated_at": int(time.time()),
                "active_window_seconds": self.config.get("active_window_seconds", 3600),
                "channels": self.config.get("channels", []),
                "total_active": self.store.total_active(),
                "dogs": self.store.active_users(),
            }
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        elif path == "/favicon.ico":
            self._send(204, "image/x-icon", b"")
        else:
            self._send(404, "text/plain", b"not found")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[config] Не удалось прочитать {CONFIG_PATH}: {e}")
    else:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[config] Создан файл настроек: {CONFIG_PATH}")
    return cfg


def main():
    ap = argparse.ArgumentParser(description="OBS Dogs of Chat plugin")
    ap.add_argument("--demo", action="store_true", help="эмуляция чата вместо Twitch")
    ap.add_argument("--port", type=int, default=None, help="HTTP порт (по умолчанию из config)")
    ap.add_argument("--host", default=None, help="IP для.bind (0.0.0.0 — доступ с других машин)")
    ap.add_argument("--channels", nargs="*", default=None, help="каналы Twitch через пробел")
    args = ap.parse_args()

    config = load_config()
    if args.channels:
        config["channels"] = args.channels
    port = args.port or config.get("http_port", 8377)
    host = args.host or config.get("http_host", "127.0.0.1")

    store = ActivityStore(config)

    if args.demo or not HAS_WEBSOCKETS:
        demo = DemoChatThread(store)
        demo.start()
    if not args.demo and HAS_WEBSOCKETS:
        irc = TwitchChatThread(store, config)
        irc.start()

    Handler.store = store
    Handler.config = config

    server = ThreadingHTTPServer((host, port), Handler)
    print("=" * 60)
    shown_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"  Собаки чата запущены:  http://{shown_host}:{port}")
    if host == "0.0.0.0":
        print(f"  Доступ из других машин:  http://IP_этой_машины:{port}")
    print(f"  В OBS: Источник -> Браузер -> URL http://{shown_host}:{port}")
    print(f"  Активность: сообщение за последние {config['active_window_seconds']} сек")
    print("  Ctrl+C — остановить.")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка...")
        server.shutdown()


if __name__ == "__main__":
    main()
