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
import base64
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer, HTTPServer
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
def log(*args):
    """print с немедленным flush — иначе systemd/journal буферизует вывод."""
    print(*args, file=sys.stderr, flush=True)


class ActivityStore:
    """Потокобезопасное хранилище: user -> last_seen_ts."""

    def __init__(self, config: dict):
        self.lock = threading.Lock()
        self.users: "OrderedDict[str, dict]" = OrderedDict()  # login -> {name,color,last}
        self.config = config

    def touch(self, login: str, display_name: str = "", color: str = "", is_mod: bool = False):
        now = time.time()
        with self.lock:
            u = self.users.get(login)
            if u is None:
                u = {"name": display_name or login, "color": color, "last": now,
                     "count": 0, "mod": False}
                self.users[login] = u
            u["last"] = now
            u["count"] += 1
            if display_name:
                u["name"] = display_name
            if color:
                u["color"] = color
            if is_mod:
                u["mod"] = True  # статус модератора запоминаем (не сбрасываем)
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
                    "mod": bool(u.get("mod")),
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

class RawWSClient:
    """Встроенный синхронный WebSocket-клиент (RFC6455), без зависимостей.
    Используется, если библиотека `websockets` не установлена."""

    def __init__(self, url, timeout=10.0):
        hostport = url[len("wss://"):].split("/")[0]
        self.host, _, port_s = hostport.partition(":")
        self.port = int(port_s or 443)
        self.timeout = timeout
        self.sock = None
        self.buf = b""

    def connect(self):
        key = base64.b64encode(os.urandom(16)).decode()
        ctx = ssl.create_default_context()
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        req = (f"GET / HTTP/1.1\r\nHost: {self.host}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WS handshake: соединение закрыто")
            resp += chunk
        head, rest = resp.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise ConnectionError(f"WS handshake failed: {status}")
        self.buf = rest
        return self

    def _read_bytes(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WS: соединение закрыто сервером")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        payload = text.encode("utf-8")
        header = struct.pack("!B", 0x81)  # FIN + text frame
        mask_key = os.urandom(4)
        ln = len(payload)
        if ln < 126:
            header += struct.pack("!B", 0x80 | ln)
        elif ln < 65536:
            header += struct.pack("!BH", 0x80 | 126, ln)
        else:
            header += struct.pack("!BQ", 0x80 | 127, ln)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask_key + masked)

    def recv(self, timeout=1.0):
        self.sock.settimeout(timeout)
        try:
            first = self._read_bytes(2)
        except socket.timeout:
            return None
        opcode = first[0] & 0x0F
        ln = first[1] & 0x7F
        if ln == 126:
            ln = struct.unpack("!H", self._read_bytes(2))[0]
        elif ln == 127:
            ln = struct.unpack("!Q", self._read_bytes(8))[0]
        data = self._read_bytes(ln) if ln else b""
        if opcode == 0x8:
            raise ConnectionError("WS: сервер закрыл соединение")
        if opcode == 0x9:  # ping -> pong (маскированный, как требует RFC для клиента)
            mk = os.urandom(4)
            self.sock.sendall(struct.pack("!BB", 0x8A, 0x80 | len(data)) + mk
                              + bytes(b ^ mk[i % 4] for i, b in enumerate(data)))
            return ""
        if opcode in (0x1, 0x2):
            return data.decode("utf-8", "replace")
        return ""

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


TWITCH_WS_URL = "wss://irc-ws.chat.twitch.tv:443"


class RawTCPClient:
    """Минимальный sync-клиент чистого TCP IRC (тот же интерфейс, что у
    websockets.sync: send/recv/close/context manager). Не требует зависимостей
    и устойчив к блокировкам WebSocket-рукопожатия."""

    def __init__(self, host, port, timeout=15.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""

    def send(self, data):
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        if not data.endswith("\r\n"):
            data += "\r\n"
        self.sock.sendall(data.encode("utf-8"))

    def recv(self, timeout=1.0):
        """Вернуть ВСЕ накопленные полные строки одним сообщением (\\r\\n между ними).

        Важно: раньше возвращалась одна строка за вызов, и PING мог «залежаться»
        в буфере, пока мы его не дочитаем, — Twitch закрывал соединение за
        молчание (ConnectionReset сразу после подключения при оживлённом канале).
        Теперь читаем всё доступное и отдаём целиком, чтобы PING обрабатывался
        немедленно."""
        self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            # таймаут истёк, но в буфере могли остаться полные линии — отдадим их
            if b"\n" in self.buf:
                out, self.buf = self.buf, b""
                return out.decode("utf-8", "replace")
            return None
        if not chunk:
            raise ConnectionError("Twitch закрыл TCP-соединение")
        self.buf += chunk
        if b"\n" not in self.buf:
            return ""  # линии пока нет — подождём следующий chunk
        # делим так, чтобы последний НЕЗАВЕРШЁННЫЙ фрагмент остался в буфере
        parts = self.buf.split(b"\n")
        self.buf = parts.pop()  # хвост без \n (может быть b"")
        lines = [p.rstrip(b"\r") for p in parts]
        while lines and not lines[-1].strip():
            lines.pop()  # не отдаём пустые хвосты (split по \r\n даёт "")
        return ("\r\n".join(l.decode("utf-8", "replace") for l in lines)
                if lines else "")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


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
        channels = [c.strip().lstrip("#").lower() for c in self.config.get("channels", []) if c.strip()]
        if not channels:
            log("[irc] Каналы не заданы — IRC-поток завершён.")
            return
        if not HAS_WEBSOCKETS:
            log("[irc] Библиотека 'websockets' не найдена в окружении сервиса. "
                "Установите в venv: /opt/dogs-chat/venv/bin/pip install websockets "
                "(или запускайте с --demo). Повторная проверка через 30 c...")
        # Вечный цикл: подключение -> чтение -> при любой ошибке переподключение
        while not self.stop_event.is_set():
            if not HAS_WEBSOCKETS:
                if self.stop_event.wait(30.0):
                    break
                continue
            nick = self.config.get("nickname") or f"justinfan{int(time.time()) % 99999}"
            try:
                self._connect_and_read(nick, channels)
            except Exception as e:
                log(f"[irc] Ошибка соединения ({type(e).__name__}: {e}). Переподключение через 5 c...")
            if self.stop_event.wait(5.0):
                break

    def _open_conn(self):
        """Открыть соединение. Надёжный порядок: TCP-IRC (6667) без зависимостей,
        затем WebSocket (sync/async). Первый успешный вариант используется до обрыва."""
        errors = []
        # Вариант 1: чистый TCP IRC на порт 6667 — не требует websockets и
        # устойчив к тому, что Twitch режет WS-трафик по HTTP-признакам.
        try:
            return RawTCPClient("irc.chat.twitch.tv", 6667)
        except Exception as e:
            errors.append(f"tcp6667: {type(e).__name__}: {e}")
        # Вариант 2: WebSocket через установленную библиотеку websockets
        if HAS_WEBSOCKETS:
            try:
                import websockets.sync.client as sc
                return sc.connect(TWITCH_WS_URL, open_timeout=15)
            except ImportError:
                try:
                    return self._async_connect_fallback()
                except Exception as e:
                    errors.append(f"ws-async: {type(e).__name__}: {e}")
            except Exception as e:
                errors.append(f"ws-sync: {type(e).__name__}: {e}")
        else:
            errors.append("websockets: библиотека не установлена")
        raise ConnectionError("Не удалось подключиться ни одним способом -> " + " | ".join(errors))

    def _connect_and_read(self, nick: str, channels: list):
        conn = self._open_conn()
        is_tcp = isinstance(conn, RawTCPClient)
        with conn:
            def send(s):
                conn.send(s.encode() if isinstance(s, str) else s)
            # Порядок: NICK -> CAP REQ -> JOIN. Пустой PASS не отправляем —
            # проверено живым тестом против irc.chat.twitch.tv:6667: строка
            # "PASS " (без токена) злит соединение без ответа, а анонимный вход
            # justinfan* работает только вообще без PASS. PASS шлём ТОЛЬКО если
            # в settings.json задан oauth_token.
            send(f"NICK {nick}")
            token = self.config.get("oauth_token", "")
            if token and not token.startswith("oauth:"):
                token = "oauth:" + token
            if token:
                send(f"PASS {token}")
            send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            for ch in channels:
                send(f"JOIN #{ch}")
            log(f"[irc] Подключено ({'TCP 6667' if is_tcp else 'WebSocket'}). Каналы: {', '.join(channels)}")
            got_msgs = 0
            last_keepalive = time.time()
            while not self.stop_event.is_set():
                try:
                    msg = conn.recv(timeout=1.0)
                except TimeoutError:
                    msg = None
                except Exception as e:
                    if type(e).__name__ in ("ConnectionClosed", "ConnectionClosedError"):
                        log("[irc] Twitch закрыл соединение (см. NOTICE выше). Переподключение через 5 c...")
                        break
                    log(f"[irc] Соединение потеряно: {type(e).__name__}: {e}. Переподключение...")
                    break
                if msg is None:
                    if time.time() - last_keepalive > 25:
                        # активный keep-alive: Twitch закрывает соединение, если
                        # от нас долго нет ни PONG, ни JOIN. Периодический повторный
                        # JOIN безвреден (сервер ответит "already in channel").
                        try:
                            for ch in channels:
                                send(f"JOIN #{ch}")
                        except Exception:
                            raise  # сокет уже мёртв — наверх, переподключение
                        last_keepalive = time.time()
                    continue
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", "replace")
                # PING от сервера может прийти в любом сообщении — отвечаем сразу,
                # иначе Twitch закрывает молчаливое соединение (~через минуту)
                if msg.startswith("PING"):
                    send("PONG :tmi.twitch.tv")
                for line in msg.split("\r\n"):
                    if line.startswith("PING"):
                        send("PONG :tmi.twitch.tv")
                        continue
                    # служебные сообщения пишем в лог — по ним видно,
                    # почему канал мог не пустить (например "No such channel")
                    if line.startswith(":") and ("NOTICE" in line or "ERR" in line.split()[1:2]):
                        log(f"[irc>] {line.strip()}")
                    parsed = parse_irc_line(line)
                    if not parsed:
                        continue
                    nick_user, tags, cmd, params, trailing = parsed
                    if cmd == "PRIVMSG" and nick_user:
                        # игнорируем ботов/сервера, если вдруг
                        login = tags.get("login", nick_user.lower())
                        if login in ("ircv3",):
                            continue
                        badges = tags.get("badges", "")
                        is_mod = "moderator/1" in badges or "broadcaster/1" in badges
                        got_msgs += 1
                        self.store.touch(login, tags.get("display-name", nick_user),
                                         tags.get("color", ""), is_mod=is_mod)
            log(f"[irc] Поток чата остановлен. Принято сообщений: {got_msgs}.")

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
# несколько "модераторов" для демонстрации будки
DEMO_MODS = ["mod_polkhan", "mod_sharik", "streamer_boss"]


class DemoChatThread(threading.Thread):
    def __init__(self, store: ActivityStore):
        super().__init__(daemon=True, name="demo-chat")
        self.store = store
        self.stop_event = threading.Event()

    def run(self):
        import random
        log("[demo] Эмуляция чата запущена (--demo).")
        # сначала "населить" историю разными давностями
        for n in DEMO_NAMES:
            offset = random.randint(0, 7200)  # 0..2 часа назад
            self.store._seed(n, time.time() - offset)
        for n in DEMO_MODS:
            self.store._seed(n, time.time() - random.randint(0, 600))
            self.store.touch(n.lower(), n, is_mod=True)
        while not self.stop_event.is_set():
            name = random.choice(DEMO_NAMES + DEMO_MODS)
            self.store.touch(name.lower(), name, is_mod=(name in DEMO_MODS))
            time.sleep(random.uniform(0.5, 3.0))

    def stop(self):
        self.stop_event.set()


# добавим метод seed в ActivityStore
def _seed(self, login, ts):
    with self.lock:
        self.users[login] = {"name": login, "color": "", "last": ts, "count": 1, "mod": False}
ActivityStore._seed = _seed


# ---------------------------------------------------------------------------
# HTTP-сервер: отдаёт HTML и JSON/SSE для Browser Source
# ---------------------------------------------------------------------------
class QuietHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer, который не печатает стектрейсы при обрыве соединения.

    OBS (и любой браузер) часто закрывает SSE-соединения или страницы без
    корректного завершения — это нормальное событие сети (ConnectionResetError /
    BrokenPipeError), а не ошибка плагина. По умолчанию socketserver вызывает
    handle_error() и льёт traceback в journalctl; мы глушим именно эти ошибки.
    """
    daemon_threads = True            # потоки не мешают остановке сервиса по Ctrl+C/systemd
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return                   # клиент оборвал соединение — тихо игнорируем
        super().handle_error(request, client_address)  # прочие ошибки — как обычно, в лог


class Handler(BaseHTTPRequestHandler):
    store: ActivityStore = None      # внедряется ниже
    config: dict = None
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        """Не падать с traceback, если клиент оборвал соединение до/во время запроса."""
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.close_connection = True

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

    def do_HEAD(self):
        # HEAD поддерживается для проверок типа `curl -I`
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

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
        elif path.startswith("/assets/"):
            # пользовательские ассеты из ui/assets (PNG-собаки, будка и т.п.)
            name = os.path.basename(path)  # защита от path traversal
            f = UI_DIR / "assets" / name
            if f.exists() and f.is_file():
                self._send(200, "image/png", f.read_bytes(),
                           extra={"Cache-Control": "no-cache"})
            else:
                self._send(404, "text/plain", b"asset not found")
        elif path == "/api/assets":
            # список доступных картинок + длительность кадра — клиент сам
            # решает, что уже нарисовано, а что пока показывать заглушкой
            adir = UI_DIR / "assets"
            files = []
            if adir.is_dir():
                try:
                    files = sorted(os.listdir(adir))
                except OSError:
                    files = []
            frame_ms = 250
            durfile = adir / "frame-duration.txt"
            if durfile.is_file():
                try:
                    txt = durfile.read_text(encoding="utf-8-sig")
                    num = "".join(ch for ch in txt.split("#")[0] if ch.isdigit())
                    if num:
                        frame_ms = max(40, min(3000, int(num)))
                except Exception:
                    pass
            data = {"files": files, "frame_duration_ms": frame_ms}
            body = json.dumps(data).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body,
                       extra={"Cache-Control": "no-store"})
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
            log(f"[config] Не удалось прочитать {CONFIG_PATH}: {e}")
    else:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"[config] Создан файл настроек: {CONFIG_PATH}")
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

    if args.demo:
        demo = DemoChatThread(store)
        demo.start()
    else:
        # В боевом режиме поднимаем ОБА источника сообщений:
        # 1) IRC-поток (TCP 6667 / WebSocket) — основной;
        # 2) демо-эмулятор, если библиотека websockets не установлена в venv
        #    сервиса, чтобы в OBS что-то было даже при проблемах с зависимостью.
        irc = TwitchChatThread(store, config)
        irc.start()
        if not HAS_WEBSOCKETS:
            log("[main] websockets не найден — дополнительно запускаю демо-поток, "
                "чтобы экран не был пустым. Установите: venv/bin/pip install websockets")
            demo = DemoChatThread(store)
            demo.start()

    Handler.store = store
    Handler.config = config

    server = QuietHTTPServer((host, port), Handler)
    log("=" * 60)
    shown_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    log(f"  Собаки чата запущены:  http://{shown_host}:{port}")
    if host == "0.0.0.0":
        log(f"  Доступ из других машин:  http://IP_этой_машины:{port}")
    log(f"  В OBS: Источник -> Браузер -> URL http://{shown_host}:{port}")
    log(f"  Активность: сообщение за последние {config['active_window_seconds']} сек")
    log("  Ctrl+C — остановить.")
    log("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка...", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
