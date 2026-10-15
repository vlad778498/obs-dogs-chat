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
import hashlib
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

# Библиотека `websockets` НЕ является зависимостью: оба транспорта (WSS 443 и
# TCP 6667) реализованы нативными клиентами в этом файле (RawWSClient/RawTCPClient).
# Флаг оставлен только для информационного лога при старте.
try:
    import websockets  # noqa: F401
    HAS_WEBSOCKETS = True
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
    "sse_max_seconds": 90,             # раз в столько секунд SSE-поток пересоздаётся
                                       # (страховка от «тихих» обрывов длинных соединений)
    "max_dogs": 60,                    # максимум собак на экране
    "nickname": "",                    # ник для IRC (пусто = justinfan random)
    "oauth_token": "",                 # опционально: oauth:... токен чата (для привилегированного чтения)
    "irc_prefer_auth": False,          # false (по умолчанию): читать чат АНОНИМНО (justinfan*) —
                                       # стабильнее; true: использовать oauth_token/nickname
    "irc_transport": "",               # ""/auto = wss 443 (основной) + tcp 6667 (резерв); "tcp"/"ws" — только один из них
    "http_port": 8377,
    "http_host": "127.0.0.1",          # 0.0.0.0 — если сервер на ВМ, а OBS на другой машине
}


# ---------------------------------------------------------------------------
# Учёт активности чатеров
# ---------------------------------------------------------------------------
def log(*args):
    """print с немедленным flush — иначе systemd/journal буферизует вывод."""
    print(*args, file=sys.stderr, flush=True)


EXCLUDE_PATH = BASE_DIR / "config" / "exclude.txt"


def parse_exclude_lines(text: str) -> set:
    """Разбирает содержимое exclude.txt в множество ников (нижний регистр).

    Правила: одна строка = один ник; пустые строки и строки с «#» в начале
    пропускаются; ведущий «@» снимается; комментарий после ника («Nick # note»)
    отбрасывается. Ник может содержать решётку внутри — тогда она сохраняется
    (сначала идёт ник, потом пробел и комментарий)."""
    out = set()
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        # «Nick  # комментарий» -> берём только первую часть до пробела
        s = s.split()[0]
        s = s.lstrip("@").strip().lower()
        if s:
            out.add(s)
    return out


class ActivityStore:
    """Потокобезопасное хранилище: user -> last_seen_ts."""

    def __init__(self, config: dict):
        self.lock = threading.Lock()
        self.users: "OrderedDict[str, dict]" = OrderedDict()  # login -> {name,color,last}
        self.config = config
        # Список исключений (config/exclude.txt): ники, которые не появятся
        # на экране. Файл перечитывается на лету (см. _excluded()), сервис
        # перезапускать не нужно.
        self._exclude_cache = set()
        self._exclude_mtime = None
        self._exclude_checked_at = 0.0

    def _excluded(self, login: str) -> bool:
        """True, если ник в списке исключений (config/exclude.txt)."""
        now = time.time()
        # перечитываем файл не чаще раза в 5 секунд (по mtime)
        if now - self._exclude_checked_at >= 5.0:
            self._exclude_checked_at = now
            try:
                mtime = EXCLUDE_PATH.stat().st_mtime
                if mtime != self._exclude_mtime:
                    self._exclude_mtime = mtime
                    self._exclude_cache = parse_exclude_lines(
                        EXCLUDE_PATH.read_text(encoding="utf-8"))
                    log(f"[exclude] Загружено исключений: {len(self._exclude_cache)} "
                        f"(файл {EXCLUDE_PATH})")
            except FileNotFoundError:
                if self._exclude_cache or self._exclude_mtime is not None:
                    log("[exclude] Файл config/exclude.txt не найден — исключений нет.")
                self._exclude_cache = set()
                self._exclude_mtime = None
            except Exception as e:
                log(f"[exclude] Не удалось прочитать {EXCLUDE_PATH}: {e}")
        return login.lower() in self._exclude_cache

    def touch(self, login: str, display_name: str = "", color: str = "", is_mod: bool = False):
        # Ник из config/exclude.txt — не попадает на экран вообще.
        if self._excluded(login):
            return
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
            # prune старых + тех, кого пользователь добавил в exclude.txt
            # уже ПОСЛЕ того, как они появились на экране (исчезают сразу)
            stale = [k for k, v in self.users.items()
                     if v["last"] < cutoff or k.lower() in self._exclude_cache]
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
    """Встроенный синхронный WebSocket-клиент (RFC6455) поверх стандартного ssl.

    Без каких-либо внешних зависимостей — работает даже там, где pip заблокирован
    (PEP 668 / externally-managed-environment). Интерфейс совместим с
    websockets.sync.client.connect: send(str|bytes), recv(timeout), close(),
    контекстный менеджер. Twitch отдаёт IRC-сообщения текстовыми кадрами;
    бинарные и фрагментированные кадры тоже поддерживаются, служебные
    ping/pong/close обрабатываются прозрачно.

    Это ОСНОВНОЙ транспорт: проверено живым тестом (tests/twitch_connect_test.py
    --wss --duration 300) — соединение на порту 443 держится стабильно весь
    прогон, тогда как plain-TCP 6667 на этой сети регулярно умирал (~2 мин).
    """

    OP_CONT, OP_TEXT, OP_BIN = 0x0, 0x1, 0x2
    OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA

    def __init__(self, url, timeout=10.0):
        hostport = url[len("wss://"):].split("/")[0]
        self.host, _, port_s = hostport.partition(":")
        self.port = int(port_s or 443)
        self.timeout = timeout
        self.sock = None
        self.buf = b""          # сырые байты из сокета (кадры + заголовок)
        self._rxacc = b""       # накопитель IRC-строк из декодированных кадров
        self._frag = b""        # собранный незавершённый фрагментированный кадр
        self._frag_op = None

    def _handshake_once(self):
        # НОВЫЙ key на каждую попытку: если Twitch/прокси отвергнет один ключ,
        # повтор с тем же самым key мог «отбиваться» той же ошибкой.
        key = base64.b64encode(os.urandom(16)).decode()
        ctx = ssl.create_default_context()
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        except OSError:
            # TLS-handshake упал — закрываем голый сокет, чтобы не течь по fd
            try:
                raw.close()
            except Exception:
                pass
            raise
        req = (f"GET / HTTP/1.1\r\nHost: {self.host}\r\nOrigin: https://twitch.tv\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.settimeout(self.timeout)
        self.sock.sendall(req.encode())
        # Читаем ответ handshake ПО БАЙТОВО (как в проверенном тесте
        # tests/twitch_connect_test.py, который держит WS 443 стабильно).
        # Прежний вариант «recv(4096) до \r\n\r\n» заглатывал из TLS-потока
        # ВСЁ, что успело прийти, вместе с первыми IRC-данными, а затем
        # терял лишнее: остаток бросался, и следующий кадр читался из
        # смещённого потока -> сервер «закрывал соединение (0 байт)».
        hdr = b""
        while not hdr.endswith(b"\r\n\r\n"):
            b = self.sock.recv(1)
            if not b:
                raise ConnectionError("WS handshake: сервер закрыл соединение на handshake")
            hdr += b
        status = hdr.split(b"\r\n", 1)[0].decode(errors="replace")
        expect = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        low = hdr.lower()
        if "101" not in status or expect.lower().encode() not in low:
            snippet = status[:200].replace("\r\n", " | ")
            raise ConnectionError(f"WS handshake failed: {snippet!r}")
        # После побайтового чтения заголовка в буфере гарантированно НОЛЬ
        # байт — поток кадров начинается ровно после \r\n\r\n.
        self.buf = b""
        self.sock.settimeout(None)   # таймауты чтения управляются внутри recv()
        return self

    def connect(self):
        """Handshake с ретраями и растущей задержкой (0.5, 1, 2 c).

        ВАЖНО — почему паузы МЕЖДУ попытками такие короткие: Twitch ограничивает
        частоту WS-handshake'ов с одного IP. Старая схема (до 4 попыток с
        паузами 2/4/8 c плюс внешний цикл ещё и спал 5 c между коннектами)
        давала ~1 попытку каждые 10-20 c НОН-СТОП — Twitch начинал молча
        обрывать соединения сразу после handshake («сервер закрыл соединение
        (0 байт)») и банл IP глубже. Логи 15.10 это ровно показывали.
        Новая схема: быстрый ретрай внутри connect (обычно 1-2 попытки
        достаточно при единичном сетевом сбое), а внешнему циклу сообщаем
        через атрибут `retry_hint`, сколько ещё поспать перед новым полным
        коннектом (см. run()).
        """
        last_err = None
        delay = 0.5
        for attempt in range(1, 4):
            try:
                conn = self._handshake_once()
                # Ключевая проверка: Twitch может ответить 101 и тут же
                # закрыть сокет (rate-limit по IP / кривой Origin). Без
                # этой проверки такой транспорт считался «успешным», и
                # сервис попадал в цикл мгновенных смертей каждые 5-10 c.
                conn.verify_alive(wait_s=5.0)
                return conn
            except (OSError, ConnectionError) as e:
                last_err = e
                try:
                    if self.sock:
                        self.sock.close()
                except Exception:
                    pass
                self.sock = None
                if attempt < 3:
                    log(f"[ws] Handshake: попытка {attempt}/3 не прошла "
                        f"({type(e).__name__}: {e}) — повтор через {delay:.1f} c...")
                    time.sleep(delay)
                    delay *= 2
        raise ConnectionError(f"WS handshake failed (3 попытки): {last_err}")

    def verify_alive(self, wait_s=5.0):
        """После handshake проверить, что сервер НЕ закрыл соединение сразу.

        Twitch при rate-limit/подозрительном Origin закрывает WS через 0-1 c
        ПОСЛЕ успешного 101. Ждём первые байты: любой входящий кадр или наш
        PING -> PONG подтверждают живость. Если за wait_s сокет закрылся
        (recv вернул пусто) — это провал транспорта, бросаем ConnectionError,
        чтобы connect() мог ретраить, а run() учёл волну отказов.
        Возвращает True, если соединение живо.
        """
        self.sock.settimeout(wait_s)
        try:
            chunk = self.sock.recv(65536)
        except (socket.timeout, TimeoutError):
            return True          # тишина, но сокет ОТКРЫТ — живой
        finally:
            self.sock.settimeout(None)
        if not chunk:
            raise ConnectionError("WS: сервер закрыл соединение сразу после handshake")
        self.buf += chunk
        return True

    # ---------- низкоуровневые операции с кадрами ----------
    def _read_exact(self, n, deadline):
        """Вернуть ровно n байт потока или бросить socket.timeout к дедлайну."""
        while len(self.buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout()
            self.sock.settimeout(min(remaining, 1.0))
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                # штатная тишина: проверим дедлайн и попробуем ещё раз
                if deadline - time.monotonic() <= 0:
                    raise
                continue
            finally:
                self.sock.settimeout(None)
            if not chunk:
                raise ConnectionError("WS: сервер закрыл соединение (0 байт)")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode, payload=b""):
        mask_key = os.urandom(4)
        ln = len(payload)
        if ln < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | ln)
        elif ln < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, ln)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, ln)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        old = self.sock.gettimeout()
        self.sock.settimeout(10.0)   # sendall не должен зависать навечно
        try:
            self.sock.sendall(header + mask_key + masked)
        finally:
            self.sock.settimeout(old)

    def _next_message(self, deadline):
        """Следующий ПРИКЛАДНЫЙ кадр (text/bin, bytes). Служебные кадры
        (ping->pong, pong, cont-фрагменты) обрабатывает сама; close — исключение."""
        while True:
            b0, b1 = self._read_exact(2, deadline)
            fin = b0 & 0x80
            op = b0 & 0x0F
            if b1 & 0x80:
                raise ConnectionError("WS: клиент не ожидает замаскированных кадров от сервера")
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._read_exact(2, deadline))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._read_exact(8, deadline))[0]
            payload = self._read_exact(ln, deadline) if ln else b""
            if op == self.OP_PING:
                self._send_frame(self.OP_PONG, payload)
                continue
            if op == self.OP_PONG:
                continue
            if op == self.OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if ln >= 2 else 0
                reason = payload[2:].decode("utf-8", "replace")[:120] if ln > 2 else ""
                raise ConnectionError(f"WS: сервер закрыл соединение (CLOSE {code} {reason!r})")
            if op in (self.OP_TEXT, self.OP_BIN):
                if fin:
                    return payload
                self._frag, self._frag_op = payload, op
                continue
            if op == self.OP_CONT:
                self._frag += payload
                if fin:
                    out, self._frag = self._frag, b""
                    return out
                continue
            raise ConnectionError(f"WS: неизвестный opcode {op:#x}")

    # ---------- публичный интерфейс (как у websockets.sync) ----------
    def send(self, data):
        if isinstance(data, bytes):
            opcode, payload = self.OP_BIN, data
        else:
            opcode, payload = self.OP_TEXT, data.encode("utf-8", "replace")
        self._send_frame(opcode, payload)

    def recv(self, timeout=1.0):
        """Вернуть ВСЕ доступные строки IRC до истечения timeout (секунд).

        Ключевое поведение (см. историю бага «сообщения видны только ~60 c»):
          * полные строки уже есть в буфере -> возвращаем НЕМЕДЛЕННО, без сна;
          * байты пришли, но строк пока нет -> вернём "" (сигнал «соединение
            живое», watchdog сбрасывается);
          * ни одного байта за timeout -> вернём None (тишина канала).
        Строки склеиваются через \\r\\n — внешний цикл сам делает split.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        lines = []
        got_any = False
        while True:
            # 1) сначала отдаём всё, что уже насобиралось (без ожидания)
            if self._msgbuf_has_lines():
                lines.extend(self._extract_msg_lines())
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                msg = self._next_message(deadline)
            except (socket.timeout, TimeoutError):
                break  # штатная тишина
            except OSError:
                raise  # настоящая ошибка сокета — наверх (переподключение)
            got_any = True
            self._rxacc += msg
            # Twitch шлёт несколько IRC-строк в одном WS-кадре (\r\n между ними);
            # как только появилась полная строка — выходим и отдаём её сразу,
            # остаток дочитается следующим вызовом recv() без задержки.
            if b"\n" in self._rxacc:
                continue
        joined = "\r\n".join(lines)
        if joined:
            return joined
        return None if not got_any else ""

    def _msgbuf_has_lines(self):
        return b"\n" in getattr(self, "_rxacc", b"")

    def _extract_msg_lines(self):
        if b"\n" not in self._rxacc:
            return []
        parts = self._rxacc.split(b"\n")
        self._rxacc = parts.pop()  # хвост без \n — ждём продолжения
        out = []
        for p in parts:
            p = p.rstrip(b"\r")
            if p.strip():
                out.append(p.decode("utf-8", "replace"))
        return out

    def close(self):
        try:
            if self.sock:
                self._send_frame(self.OP_CLOSE)
        except Exception:
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


TWITCH_WS_HOST = "irc-ws.chat.twitch.tv"
TWITCH_WS_PORT = 443
TWITCH_WS_URL = f"wss://{TWITCH_WS_HOST}:{TWITCH_WS_PORT}"
TWITCH_TCP_HOST = "irc.chat.twitch.tv"
TWITCH_TCP_PORT = 6667


class RawTCPClient:
    """Минимальный sync-клиент чистого TCP IRC (тот же интерфейс, что у
    websockets.sync: send/recv/close/context manager). Не требует зависимостей
    и устойчив к блокировкам WebSocket-рукопожатия."""

    def __init__(self, host, port, timeout=15.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""
        # Таймаут записи: sendall не должен зависать навечно, если TCP-путь
        # встали (half-open / перегруженный буфер). 10 c достаточно для IRC.
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
        except OSError:
            pass

    def send(self, data):
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        if not data.endswith("\r\n"):
            data += "\r\n"
        old = self.sock.gettimeout()
        self.sock.settimeout(10.0)
        try:
            self.sock.sendall(data.encode("utf-8"))
        finally:
            self.sock.settimeout(old)

    def _extract_lines(self):
        """Забрать из буфера ВСЕ полные строки (каждая завершена \\n).

        Делим по b"\\n": последний фрагмент без переводы строки — незавершённая
        линия, она ОСТАЁТСЯ в буфере до следующего пакета.
        """
        if b"\n" not in self.buf:
            return []
        parts = self.buf.split(b"\n")
        self.buf = parts.pop()  # хвост без \n — ждём продолжения
        lines = [p.rstrip(b"\r") for p in parts]
        return [l.decode("utf-8", "replace") for l in lines if l.strip()]

    def recv(self, timeout=1.0):
        """Вернуть ВСЕ полные IRC-строки, доступные прямо сейчас.

        КЛЮЧЕВОЕ ПОВЕДЕНИЕ (исправление бага «сообщения приходят только ~60 c,
        потом молчание до переподключения»): если в буфере УЖЕ есть полные
        строки — они возвращаются НЕМЕДЛЕННО, без ожидания таймаута. Раньше
        recv() всегда ждал полный `timeout`, даже когда данные готовы; из-за
        этого обработка пачек растягивалась, и при плотном потоке сообщений
        очередь постоянно отставала.

        Если полных строк нет — читаем сокет до `timeout`:
          * пришли байты, но линии ещё неполные -> вернём "" (сигнал
            «байты шли, соединение живое», watchdog сбрасывается);
          * ни одного байта за timeout -> вернём None (тишина).

        socket.timeout (Python <3.10) и TimeoutError (>=3.10) — штатная
        ситуация тишины, а НЕ ошибка сокета; TimeoutError является подклассом
        OSError, поэтому порядок except здесь принципиален.
        """
        # 1) Готовые строки есть — отдаём сразу, не спим.
        lines = self._extract_lines()
        if lines:
            return "\r\n".join(lines)
        # 2) Читаем до дедлайна, собирая весь доступный трафик.
        deadline = time.monotonic() + max(0.0, timeout)
        got_any = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                break  # таймаут — штатно, в чате/на канале тихо
            except OSError:
                raise  # настоящая ошибка сокета — наверх (переподключение)
            finally:
                # send() обязан блокироваться до отправки, а не падать по
                # короткому read-таймауту — возвращаем бесконечный таймаут
                self.sock.settimeout(None)
            if not chunk:
                raise ConnectionError("Twitch закрыл TCP-соединение")
            got_any = True
            self.buf += chunk
            # Как только набрали полную строку — выходим и отдаём её сразу,
            # остаток дочитается следующим вызовом (без задержки).
            if b"\n" in self.buf:
                break
        lines = self._extract_lines()
        if lines:
            return "\r\n".join(lines)
        return None if not got_any else ""

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# Максимально допустимый простой БЕЗ ЛЮБОГО входящего пакета от сервера.
# После него соединение считается мёртвым (см. watchdog в _connect_and_read).
# ВАЖНО: watchdog рвёт соединение ТОЛЬКО если наш собственный PING остался без
# ответа. Пока Twitch отвечает на PING — TCP-путь живой и сообщение чата может
# прийти в любую секунду, поэтому соединение НЕ разрывается, даже если в чате
# тихо несколько минут подряд. Значение можно переопределить в settings.json:
# "irc_watchdog_seconds".
WATCHDOG_S_DEFAULT = 120


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
        self.last_transport = "?"   # фактический транспорт текущего соединения (для лога)
        # --- диагностика "соединение живо?" (пишется в journal каждые 120 c) ---
        self.connected_at = None       # когда установлено текущее соединение
        self.conn_id = 0               # счётчик переподключений
        self.total_msgs = 0            # PRIVMSG за всё время работы потока
        self.msgs_this_conn = 0        # PRIVMSG за текущее соединение
        self.last_msg_ts = None        # время последнего принятого сообщения
        self.last_ping_pong = None     # время последнего PONG на серверный PING
        self.last_pong_reply = None    # время ответа PONG от Twitch на НАШ PING
        self.last_alive_log = 0.0      # когда последний раз печатали [alive]
        # --- расширенная диагностика (коммит 9d34f0e, которого не было в репо) ---
        self.rx_bytes = 0              # байт принято за текущее соединение
        self.rx_lines = 0              # строк получено за текущее соединение
        self.rx_last_ts = None         # когда пришёл ЛЮБОЙ пакет (не только PRIVMSG)
        self.my_pongs = 0              # сколько раз мы ответили PONG на PING сервера
        self.server_pings = 0          # сколько PING прислал сервер
        self.last_privmsg_line = ""    # последняя полная строка PRIVMSG (для лога)
        self._rx_buf = 0               # байт в буфере conn (неполные строки), для [diag]
        self.irc_cmds_seen = {}        # счётчик всех IRC-команд от сервера за соединение
                                       # (JOIN/PART/NAMES/353/NOTICE/PRIVMSG...) — по нему
                                       # видно, жив ли канал и что именно шлёт Twitch
        # --- сторожевой таймер (watchdog) ---
        # Когда последний раз УСПЕШНО отправили что-либо в сокет. Если Twitch
        # тихо убивает TCP-соединение (half-open: от нас принимают, нам не
        # шлют), то send() на живом сокете НЕ бросает исключение и молча
        # «успешен» бесконечно — поэтому просто PING каждые 25 c проблему не
        # лечат. Единственный честный признак жизни — подтверждение от сервера
        # (любой входящий пакет). Если его нет дольше WATCHDOG_S секунд, рвём
        # сокет принудительно (close -> recv кинет ошибку -> переподключение).
        self.last_send_ok = None

    def run(self):
        channels = [c.strip().lstrip("#").lower() for c in self.config.get("channels", []) if c.strip()]
        if not channels:
            log("[irc] Каналы не заданы — IRC-поток завершён.")
            return
        # Вечный цикл: подключение -> чтение -> при любой ошибке переподключение.
        # Библиотека `websockets` больше НЕ нужна ни в одном режиме: и WSS 443,
        # и TCP 6667 работают нативными клиентами (RawWSClient / RawTCPClient).
        # ВАЖНО: при заданном oauth_token NICK ОБЯЗАН совпадать с логином
        # владельца токена (в нижнем регистре). Twitch проверяет пару
        # NICK+PASS и на несоответствие отвечает
        # "NOTICE * :Improperly formatted auth" и молча не пускает в канал.
        token = (self.config.get("oauth_token") or "").strip()
        cfg_nick = (self.config.get("nickname") or "").strip().lower()
        if token and not cfg_nick:
            log("[irc] В settings.json есть oauth_token, но пустой nickname — "
                "для авторизованного входа NICK должен совпадать с логином "
                "владельца токена. Временный fallback: анонимный justinfan*.")
        # АНОНИМНЫЙ вход justinfan* — ПЕРВЫЙ вариант по умолчанию.
        # Проверено живым тестом (tests/twitch_connect_test.py --wss, 300 c
        # без единого обрыва): анонимное WS-соединение к Twitch держится
        # стабильно. Авторизованный вход тем же токеном/NICK наоборот
        # умирал волнами «сервер закрыл соединение (0 байт)» сразу после
        # connect() — см. логи 14–15.10. Для простого НАБЛЮДЕНИЯ за чатом
        # авторизация не нужна вовсе; если вы хотите писать в чат или
        # читать закрытые каналы — явно поставьте в settings.json
        # "irc_prefer_auth": true.
        prefer_auth = bool(self.config.get("irc_prefer_auth", False))
        if token and not prefer_auth:
            log("[irc] oauth_token в конфиге есть, но для наблюдения он НЕ нужен — "
                "подключаюсь анонимно (justinfan*). Это лечит мгновенные обрывы "
                "WS-соединений у Twitch. Чтобы включить авторизованный вход: "
                "\"irc_prefer_auth\": true в settings.json.")
            token = ""
        nick = cfg_nick if (token and cfg_nick) else (cfg_nick or f"justinfan{int(time.time()) % 99999}")
        if token:
            log(f"[irc] Авторизованный вход: NICK={nick} + PASS oauth:***")
        backoff = 5.0   # пауза перед новым коннектом; растёт при повторных
                        # смертях, чтобы НЕ долбить Twitch handshake'ами
                        # (частые handshake с одного IP — причина банов edge)
        while not self.stop_event.is_set():
            try:
                self._connect_and_read(nick, channels)
                # Соединение отработало и завершилось (штатно или по ошибке чтения),
                # но ДОЛГО — значит текущий транспорт в целом рабочий: снимаем
                # счётчик сбоев handshake и бэкофф, чтобы единичные NetworkError
                # не включали принудительный tcp-first.
                if getattr(self, "_conn_was_healthy", False):
                    self._ws_handshake_fails = 0
                    backoff = 5.0
            except Exception as e:
                self._note_transport_failure(e)
                log(f"[irc] Ошибка соединения ({type(e).__name__}: {e}). "
                    f"Переподключение через {backoff:.0f} c...")
            # Короткая жизнь соединения (<20 c) = волна отказов транспорта:
            # увеличиваем паузу до 30 c максимум (обратная сторона прежнего
            # бага — сервис долбил WS каждые 5 c и получал всё больше обрывов).
            if getattr(self, "_last_conn_lifetime", 999) < 20:
                backoff = min(backoff * 2, 30.0)
            if self.stop_event.wait(backoff):
                break

    def _open_conn(self):
        """Открыть соединение. Порядок по умолчанию: WebSocket 443 (нативный
        RawWSClient — без зависимостей), затем plain-TCP 6667 как резерв.

        ВАЖНО (по живым тестам на этой сети, tests/twitch_connect_test.py):
        WSS на порту 443 держится стабильно часами, тогда как TCP 6667
        регулярно умирал каждые ~2 минуты (NAT/провайдер режет long-lived
        соединения на нестандартном порту). Поэтому WS — ОСНОВНОЙ транспорт,
        а TCP — запасной (например, если TLS-инспекция мешает handshake).
        Библиотека `websockets` НЕ нужна ни для чего: используется наш
        встроенный RFC6455-клиент. Порядок можно переопределить в
        settings.json: "irc_transport": "tcp"|"ws" (пусто = авто = ws-first)."""
        errors = []
        order = (self.config.get("irc_transport") or "").strip().lower()
        if order not in ("", "tcp", "ws", "auto"):
            log(f"[irc] Неизвестный irc_transport='{order}', использую авто (wss 443)")
        want_tcp = order in ("", "tcp", "auto")
        want_ws = order in ("", "ws", "auto")

        # Вариант 1: WebSocket wss://irc-ws.chat.twitch.tv:443 — нативный клиент.
        if want_ws:
            try:
                conn = RawWSClient(TWITCH_WS_URL, timeout=15.0).connect()
                self.last_transport = "WebSocket wss://443"
                return conn
            except Exception as e:
                errors.append(f"wss-native: {type(e).__name__}: {e}")
        # Вариант 2: чистый TCP IRC на порт 6667 — резерв (без TLS/WS).
        if want_tcp:
            try:
                conn = RawTCPClient(TWITCH_TCP_HOST, TWITCH_TCP_PORT)
                self.last_transport = "TCP 6667"
                return conn
            except Exception as e:
                errors.append(f"tcp6667: {type(e).__name__}: {e}")
        raise ConnectionError("Не удалось подключиться ни одним способом -> " + " | ".join(errors))

    def _note_transport_failure(self, err):
        """Если WS нестабилен (не проходит handshake ИЛИ сервер закрывает
        соединение сразу после connect'а — «0 байт»), переключить ПОРЯДОК
        транспортов: TCP 6667 становится основным, WS — запасным. Состояние
        живёт только в памяти процесса и сбрасывается рестартом сервиса.

        Логика порога: connect() сам делает до 4 попыток с экспоненциальной
        паузой; если вся эта процедура провалилась — это уже «одна волна».
        Две волны подряд (или две мгновенные смерти соединения <5 c) —
        достаточно, чтобы признать WS на этой сети нерабочим и уйти на TCP.
        Обратное переключение: соединение, прожившее >60 c, обнуляет счётчик
        (см. run() / _conn_was_healthy), поэтому временный сбой WS не приводит
        к вечному tcp-first."""
        s = str(err).lower()
        ws_broken = ("handshake" in s or "закрыл соединение" in s
                     or "close" in s or "0 байт" in s)
        if not ws_broken:
            return
        self._ws_handshake_fails = getattr(self, "_ws_handshake_fails", 0) + 1
        if self._ws_handshake_fails >= 2 and getattr(self, "_transport_order", "ws-first") == "ws-first":
            self._transport_order = "tcp-first"
            log("[irc] WebSocket 443 нестабилен (2 волны отказов подряд) — "
                "переключаюсь на основной транспорт TCP 6667 (WS останется "
                "запасным). Типичные причины: TLS-инспекция/блокировка "
                "handshake на 443 или временный бан IP у edge-ноды Twitch. "
                "После того как любое соединение проживёт >60 c, WS снова "
                "станет основным автоматически.")

    def _open_conn_ordered(self):
        """Открыть соединение с учётом динамического порядка транспортов."""
        if getattr(self, "_transport_order", "ws-first") == "tcp-first":
            order = (self.config.get("irc_transport") or "").strip().lower()
            if order in ("", "auto", "tcp"):
                try:
                    conn = RawTCPClient(TWITCH_TCP_HOST, TWITCH_TCP_PORT)
                    self.last_transport = "TCP 6667"
                    return conn
                except Exception as e:
                    self.errors_list = [f"tcp6667(preferred): {type(e).__name__}: {e}"]
            try:
                conn = RawWSClient(TWITCH_WS_URL, timeout=15.0).connect()
                self.last_transport = "WebSocket wss://443"
                return conn
            except Exception as e:
                raise ConnectionError(
                    "Не удалось подключиться (tcp-first) -> "
                    + " | ".join(getattr(self, "errors_list", [])) + f" | wss: {type(e).__name__}: {e}")
        return self._open_conn()

    def _log_alive(self, force=False):
        """Каждые 120 секунд писать в journal сводку: живо ли соединение и
        идут ли сообщения. Если что-то из этого перестанет работать — будет
        видно НЕВОЗМОЖНО пропустить прямо в `journalctl -u dogs-chat -f`.

        ВАЖНОЕ ДОПОЛНЕНИЕ: раньше по логу нельзя было отличить «Twitch молчит»
        (нет сообщений в чате / сервер не шлёт трафик) от «наш код потерял
        сообщения». Теперь пишутся раздельно: любой входящий пакет [diag] и
        каждое принятое сообщение чата [msg]."""
        now = time.time()
        if not force and now - self.last_alive_log < 120:
            return
        self.last_alive_log = now
        if self.connected_at is None:
            log("[alive] IRC-соединение НЕ установлено (переподключение/ошибка).")
            return
        age = int(now - self.connected_at)
        since_msg = "-" if self.last_msg_ts is None else f"{int(now - self.last_msg_ts)} c назад"
        since_pp = "-" if self.last_ping_pong is None else f"{int(now - self.last_ping_pong)} c назад"
        since_rx = "-" if self.rx_last_ts is None else f"{int(now - self.rx_last_ts)} c назад"
        since_pr = ("-" if self.last_pong_reply is None
                    else f"{int(now - self.last_pong_reply)} c назад")
        active = self.store.total_active()
        log(f"[alive] Соединение #{self.conn_id} ({self.last_transport}) активно {age} c; "
            f"PRIVMSG за это соединение: {self.msgs_this_conn} (всего {self.total_msgs}); "
            f"последнее сообщение: {since_msg}; последний PONG на PING сервера: {since_pp}; "
            f"ответ Twitch на наш PING: {since_pr}; "
            f"активных чатеров сейчас: {active}")
        log(f"[diag] За соединение #{self.conn_id}: байт принято {self.rx_bytes}, строк {self.rx_lines}; "
            f"любой входящий пакет был: {since_rx}; PING от сервера: {self.server_pings}, "
            f"наших PONG: {self.my_pongs}. "
            + ("ВЫВОД: Twitch НЕ присылает нам трафик (см. [msg]/[diag]) — причина вне нашего кода."
               if self.rx_last_ts is None or now - self.rx_last_ts > 60
               else "ВЫВОД: пакеты от Twitch приходят — если чатеров нет, значит в чате тихо или теряются PRIVMSG.")
            + (f" Буфер сокета: {self._rx_buf} байт непереведённых строк; "
               f"всего переподключений: {self.conn_id}."))
        # --- канал до браузера OBS (SSE). Если [msg] идут, а собаки нет —
        # причина здесь: sse_clients=0 значит браузер НЕ держит /api/stream
        # (URL источника/перезагрузка страницы), last push давно -> поток умер.
        # Handler определён ниже в этом же модуле — берём из глобального
        # словаря на момент вызова (импорт self-модуля недопустим: сервис
        # запускается как __main__).
        _H = globals().get("Handler")
        if _H is None:
            return
        since_push = "никогда" if _H.last_sse_push is None else f"{int(now - _H.last_sse_push)} c назад"
        log(f"[ui] SSE-клиентов: {_H.sse_clients}; последний снимок браузеру: {since_push}"
            + (" — ВНИМАНИЕ: ни одного подключённого клиента /api/stream! "
               "Сообщения до сервера доходят, но OBS их не запрашивает: "
               "проверьте URL browser source и перезагрузите источник."
               if _H.sse_clients == 0 and self.msgs_this_conn > 0 else ""))
        # какие команды вообще приходили от сервера за это соединение. Если тут
        # есть JOIN/PART/NAMES/353, но нет PRIVMSG — канал жив и Twitch реально
        # не шлёт сообщения чата (типично для анонимного justinfan-подключения).
        cmds = ", ".join(f"{k}:{v}" for k, v in sorted(self.irc_cmds_seen.items(),
                                                       key=lambda kv: -kv[1])[:12]) or "-"
        log(f"[cmds] Команды от сервера за соединение #{self.conn_id}: {cmds}")

    def _connect_and_read(self, nick: str, channels: list):
        conn = self._open_conn_ordered()

        with conn:
            # TCP-клиент не умеет verify_alive (у него нет handshake-фазы) —
            # проверка только для WS.
            if hasattr(conn, "verify_alive"):
                conn.verify_alive(wait_s=5.0)

            # ВАЖНО (корень бага «WS 443: сервер закрыл соединение (0 байт)»
            # мгновенно после JOIN — логи 02:51): отправку надо выбирать по
            # ТИПУ соединения, а не по типу данных. RawWSClient.send(bytes)
            # упаковывает payload в BINARY-кадр (opcode 0x2), а Twitch требует
            # TEXT (0x1) и молча рвёт соединение на бинарных кадрах. Старый код
            # делал s.encode() для ВСЕХ транспортов -> WS всегда получал BIN ->
            # handshake проходил, а умирало соединение сразу после NICK/JOIN.
            # RawTCPClient принимает и str, и bytes — ему передаём как есть.
            is_ws = hasattr(conn, "verify_alive")

            def send(s):
                conn.send(s if is_ws else (s.encode() if isinstance(s, str) else s))
            # Порядок: NICK -> CAP REQ -> JOIN. Пустой PASS не отправляем —
            # проверено живым тестом против irc.chat.twitch.tv:6667: строка
            # "PASS " (без токена) злит соединение без ответа, а анонимный вход
            # justinfan* работает только вообще без PASS. PASS шлём ТОЛЬКО если
            # в settings.json задан oauth_token.
            token = (self.config.get("oauth_token") or "").strip()
            # Допускаем в settings.json любой из форматов, которые люди
            # копируют из генераторов токенов: "abc123...", "oauth:abc123..."
            # или "password=oauth:abc123..." (формат чужих сниппетов).
            if token.lower().startswith("password="):
                token = token.split("=", 1)[1].strip()
            if token.lower().startswith("oauth:"):
                token = token[len("oauth:"):].strip()
            # Порядок NICK/PASS/CAP. Для авторизованного входа шлём строго
            # PASS -> NICK -> CAP REQ: проверено живым тестом против
            # irc.chat.twitch.tv:6667 — только в этом порядке Twitch принимает
            # токен; при NICK раньше PASS сервер успевает зарегистрировать
            # соединение как анонимное и отвечает
            # "NOTICE * :Improperly formatted auth".
            # Для анонимного входа PASS вообще не отправляем — пустая строка
            # "PASS " злит соединение без ответа (тоже проверено вживую).
            if token:
                send(f"PASS oauth:{token}")
                send(f"NICK {nick}")
            else:
                send(f"NICK {nick}")
            send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            for ch in channels:
                send(f"JOIN #{ch}")
            log(f"[irc] Подключено ({self.last_transport}). Каналы: {', '.join(channels)}")
            # --- счётчики для диагностики (см. _log_alive) ---
            self.conn_id += 1
            self.connected_at = time.time()
            self.msgs_this_conn = 0
            self.rx_bytes = 0
            self.rx_lines = 0
            self.rx_last_ts = None
            self.server_pings = 0
            self.my_pongs = 0
            self.last_pong_reply = None
            self.irc_cmds_seen = {}
            self._log_alive(force=True)   # сразу при подключении — видно, что всё поднялось
            got_msgs = 0
            last_keepalive = time.time()
            last_rx_wall = time.time()      # когда был ЛЮБОЙ входящий байт (wall clock)
            idle_logged_60 = False          # чтобы [stall] не повторялся каждую секунду
            self.last_send_ok = time.time()
            # сколько подряд секунд (тиков recv с таймаутом 1 c) сокет НЕ
            # прислал ни одного байта — честный признак half-open
            silent_ticks = 0
            watchdog_s_static = max(40, int(self.config.get(
                "irc_watchdog_seconds", WATCHDOG_S_DEFAULT)))
            while not self.stop_event.is_set():
                # ВАЖНО: _log_alive() вызывается ТОЛЬКО когда тик реально
                # что-то прочитал или сокет молчал полную секунду (см. ниже).
                # Если conn.recv() вернул "" (байты пришли, но полных строк
                # пока нет), цикл продолжался БЕЗ sleep и крутился на 100% CPU,
                # успевая затащить в лог тысячи строк [alive]/[diag] за минуту.
                try:
                    msg = conn.recv(timeout=1.0)
                except (TimeoutError, socket.timeout):
                    # RawTCPClient.recv теперь сам инкапсулирует таймаут (None),
                    # но сторонние коннекторы (websockets) могут кинуть — штатно.
                    msg = None
                except Exception as e:
                    if type(e).__name__ in ("ConnectionClosed", "ConnectionClosedError"):
                        log("[irc] Twitch закрыл соединение (см. NOTICE выше). Переподключение через 5 c...")
                        break
                    log(f"[irc] Соединение потеряно: {type(e).__name__}: {e}. Переподключение...")
                    break
                if msg is None:
                    self._log_alive()      # раз в 2 минуты строчка [alive] в journal
                    now_w = time.time()
                    # recv вернул None => за ПОЛНУЮ секунду из сокета не пришло
                    # НИ ОДНОГО байта (новый recv() вычитывает весь доступный
                    # трафик до упора, так что "висящих" байт в буфере больше
                    # нет — см. исправление ложного срабатывания watchdog).
                    silent_ticks += 1
                    if silent_ticks > watchdog_s_static + 5:
                        log(f"[irc] Watchdog: {silent_ticks} c сокет НЕ прислал "
                            f"ни одного байта (наши PING без ответа). Соединение "
                            f"мертво — принудительный разрыв и переподключение.")
                        conn.close()
                        break
                    # --- «тишина» больше НЕ рвёт соединение ---
                    # Раньше при 60-90 секундах без входящих байтов мы
                    # принудительно переподключались. Это было избыточно: пока
                    # Twitch отвечает на наши PING, TCP-путь живой, и первое же
                    # сообщение чата приходит по этому же соединению. Разрыв
                    # «на всякий случай» лишь создавал окна в 5 секунд без чата.
                    # Теперь предупреждаем в лог, но сидим и ждём; разрыв
                    # происходит только настоящим watchdog'ем выше — если
                    # сервер перестал ОТВЕЧАТЬ на наши PING (half-open).
                    if now_w - last_rx_wall > 60 and not idle_logged_60:
                        idle_logged_60 = True
                        cmds = ", ".join(f"{k}:{v}" for k, v in sorted(
                            self.irc_cmds_seen.items(), key=lambda kv: -kv[1])[:12]) or "-"
                        log(f"[stall] Тишина 60+ c (чат молчит или Twitch не шлёт "
                            f"PRIVMSG). Соединение НЕ рвём — ждём ответов на PING. "
                            f"Команды за соединение: {cmds}")
                    if now_w - last_keepalive > 30:
                        # Активный keep-alive: PING каждые 30 c. Twitch отвечает
                        # PONG — это обновляет rx_last_ts/last_rx_wall и
                        # обнуляет silent_ticks, поэтому живое соединение
                        # watchdog НЕ тронет никогда. Повторный JOIN не шлём:
                        # он был «диагностикой», но плодил лишние JOIN/353/366
                        # и шум в логах.
                        try:
                            send("PING :dogs-chat")
                            self.last_send_ok = time.time()
                        except Exception:
                            raise  # сокет уже мёртв — наверх, переподключение
                        last_keepalive = time.time()
                    continue
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", "replace")
                # УЧЁТ ЛЮБОГО входящего пакета — ключевая диагностика: позволяет
                # отличить «Twitch молчит» от «мы потеряли сообщения».
                self.rx_bytes += len(msg)
                self.rx_last_ts = time.time()
                last_rx_wall = time.time()
                silent_ticks = 0   # байты из сокета идут — соединение живое
                idle_logged_60 = False
                # размер буфера соединения (для [diag]; websockets его не имеет)
                self._rx_buf = len(getattr(conn, "buf", b""))
                if not msg:
                    # байты пришли, но полных строк ещё нет — идём читать дальше
                    # БЕЗ _log_alive (см. защиту от busy-loop выше)
                    continue
                self._log_alive()  # раз в 2 минуты строчка [alive] в journal
                # PING от сервера может прийти в любом сообщении — отвечаем сразу,
                # иначе Twitch закрывает молчаливое соединение (~через минуту)
                if "PING" in msg:
                    for line in msg.split("\r\n"):
                        # Определяем команду строго по синтаксису IRC, а не по
                        # вхождению подстроки "PING": раньше строка вида
                        # ":nick!u@h PRIVMSG #ch :привет, PING!" ошибочно
                        # считалась серверным PING — мы зря шали PONG, а
                        # реальная обработка строки пропускалась (сообщение
                        # «терялось»). Теперь сверяем токен команды после
                        # префикса/тегов. PING может приходить с префиксом,
                        # например ":tmi.twitch.tv PING :irc.chat.twitch.tv" —
                        # на такой тоже отвечаем немедленно, иначе Twitch
                        # ровно через ~120 c молчания закрывает соединение сам
                        # («сообщения перестают появляться, приходится
                        # переподключаться»).
                        try:
                            parsed_hdr = parse_irc_line(line)
                        except Exception:
                            parsed_hdr = None
                        cmd_hdr = parsed_hdr[2] if parsed_hdr else ""
                        if cmd_hdr == "PING":
                            send("PONG :tmi.twitch.tv")
                            self.server_pings += 1
                            self.my_pongs += 1
                            self.last_ping_pong = time.time()
                for line in msg.split("\r\n"):
                    if not line.strip():
                        continue
                    self.rx_lines += 1
                    try:
                        parsed = parse_irc_line(line)
                    except Exception:
                        parsed = None
                    if not parsed:
                        continue
                    nick_user, tags, cmd, params, trailing = parsed
                    if cmd == "PING":
                        continue  # уже обработано выше (ответ PONG отправлен)
                    # учёт ВСЕХ команд от сервера — для [cmds] в диагностике
                    self.irc_cmds_seen[cmd] = self.irc_cmds_seen.get(cmd, 0) + 1
                    # служебные сообщения пишем в лог — по ним видно,
                    # почему канал мог не пустить (например "No such channel")
                    if line.startswith(":") and (cmd == "NOTICE" or (params and str(params[0]).startswith("4"))):
                        log(f"[irc>] {line.strip()}")
                        # частая причина «сервис работает, а чата нет»: неверная
                        # авторизация. Объясняем по-человечески, что исправить.
                        if "Improperly formatted auth" in line or "Login authentication failed" in line:
                            # Разворачиваем причину по фактическим данным конфига —
                            # пользователю не надо гадать, что именно не так.
                            tok = (self.config.get("oauth_token") or "").strip()
                            nk = (self.config.get("nickname") or "").strip()
                            reasons = []
                            if not tok:
                                reasons.append("oauth_token пустой")
                            else:
                                core = tok.split("=", 1)[-1].replace("oauth:", "").strip()
                                if len(core) != 30 or not core.isalnum():
                                    reasons.append(
                                        f"oauth_token выглядит неверно (длина {len(core)}, "
                                        "нужно 30 букв/цифр без префиксов)")
                            if tok and not nk:
                                reasons.append("пустой nickname (должен быть логин владельца токена)")
                            log("[irc] ВНИМАНИЕ: Twitch отверг авторизацию."
                                + (" Возможные причины: " + "; ".join(reasons) + "." if reasons else
                                   " Токен формально корректен, но сервер его не принимает: "
                                   "скорее всего он выпущен БЕЗ scope chat:read, для другого "
                                   "client id, просрочен или отозван.")
                                + " Что делать: перегенерируйте токен со scope chat:read для "
                                  "аккаунта из nickname; либо удалите oauth_token/nickname — "
                                  "сервис перейдёт на анонимный вход justinfan*.")
                    if cmd == "PONG":
                        # Twitch ответил на наш собственный PING (:dogs-chat)
                        # — значит TCP-путь туда-обратно живой даже при полной
                        # тишине в чате; watchdog не должен рвать такое соединение
                        self.last_pong_reply = time.time()
                    if cmd == "PRIVMSG" and nick_user:
                        # игнорируем ботов/сервера, если вдруг
                        login = tags.get("login", nick_user.lower())
                        if login in ("ircv3",):
                            continue
                        badges = tags.get("badges", "")
                        is_mod = "moderator/1" in badges or "broadcaster/1" in badges
                        got_msgs += 1
                        self.total_msgs += 1
                        self.msgs_this_conn += 1
                        self.last_msg_ts = time.time()
                        self.last_privmsg_line = line[:200]
                        # каждое принятое сообщение — одной строкой в journal.
                        # Если чатерс пишет, а собаки не появляются — здесь он
                        # виден, значит проблема дальше по коду; если строк
                        # [msg] нет вовсе — до нас не доходит трафик от Twitch.
                        log(f"[msg] {login}: {(trailing or '')[:60]}")
                        self.store.touch(login, tags.get("display-name", nick_user),
                                         tags.get("color", ""), is_mod=is_mod)
            log(f"[irc] Поток чата остановлен. Принято сообщений за это соединение: {got_msgs}.")
            lifetime = (time.time() - self.connected_at) if self.connected_at else 0
            self._last_conn_lifetime = lifetime
            # «Здоровым» считаем соединение, которое продержалось дольше минуты —
            # для него транспорт точно рабочий (см. сброс _ws_handshake_fails в run())
            if lifetime > 60:
                self._conn_was_healthy = True
            elif getattr(self, "last_transport", "").startswith("WebSocket"):
                # Соединение УМЕРЛО менее чем через минуту после connect().
                # Это ровно тот паттерн из логов 15.10: «Подключено (WebSocket
                # wss://443)» -> через 0-1 c «сервер закрыл соединение (0 байт)».
                # Handshake формально прошёл, поэтому run() ловит не исключение
                # и _note_transport_failure() не вызывается — счётчик волн WS
                # никогда не набирался и tcp-first-откат не срабатывал.
                # Учитываем такую мгновенную смерть как провал WS здесь.
                self._ws_handshake_fails = getattr(self, "_ws_handshake_fails", 0) + 1
                if self._ws_handshake_fails == 1:
                    log("[irc] WS-соединение умерло сразу после подключения "
                        "(менее минуты). При повторе такого же перехода на "
                        "TCP 6667 как основной транспорт.")
                elif self._ws_handshake_fails >= 2 and getattr(self, "_transport_order", "ws-first") == "ws-first":
                    self._transport_order = "tcp-first"
                    log("[irc] WebSocket 443 нестабилен (несколько мгновенных "
                        "смертей подряд) — переключаюсь на основной транспорт "
                        "TCP 6667 (WS останется запасным). После соединения, "
                        "прожившего >60 c, WS снова станет основным сам.")
            self.connected_at = None  # чтобы [alive] честно писал «не установлено»

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
    # --- диагностика UI-канала (SSE): «IRC молотит, а собаки не появляются»
    # раньше нельзя было отличить от «браузер OBS потерял соединение». Теперь
    # видно: сколько клиентов /api/stream подключено прямо сейчас и когда был
    # последний УСПЕШНЫЙ снимок, отданный браузеру. Пишется в [diag] потока IRC.
    sse_clients = 0
    last_sse_push = None

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
        # КРИТИЧНО для OBS BrowserSource: без no-cache CEF/Chromium отдаёт
        # index.html и dogs.js из HTTP-кэша. После любой правки UI браузер
        # в OBS продолжает крутить СТАРУЮ версию скрипта — все фиксы «не
        # работают» даже после git pull и systemctl restart. Раньше кэш был
        # только у /assets/*, из-за чего и рождалась легенда «собаки
        # появляются лишь минуту».
        self.send_header("Cache-Control", "no-store, must-revalidate")
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
            # Максимальная длительность одного SSE-соединения. После неё поток
            # сам завершается; EventSource в браузере/OBS мгновенно открывает
            # новое. Это «страховка от 120 секунд»: даже если где-то по пути
            # (NAT, файрвол, кэширующий прокси, CEF) молчаливое долгое
            # соединение тихо убьют — данные перестанут зависать, соединение
            # регулярно пересоздаётся заново.
            max_life = max(30, int(self.config.get("sse_max_seconds", 90)))
            interval = max(1, int(self.config.get("refresh_ui_seconds", 5)))
            window = self.config.get("active_window_seconds", 3600)
            start = time.monotonic()
            Handler.sse_clients += 1
            try:
                while time.monotonic() - start < max_life:
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
                    # снимок РЕАЛЬНО отправлен браузеру — фиксируем для [diag]
                    Handler.last_sse_push = time.time()
                    time.sleep(interval)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # клиент ушёл — штатно
            finally:
                Handler.sse_clients -= 1
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
        # В боевом режиме IRC-поток — единственный источник сообщений.
        # Транспорт по умолчанию: WebSocket wss://443 (нативный клиент,
        # без зависимостей), резерв — TCP 6667. См. _open_conn().
        irc = TwitchChatThread(store, config)
        irc.start()
        if not HAS_WEBSOCKETS:
            log("[main] Библиотека 'websockets' не установлена — это НЕ ошибка: "
                "оба транспорта (WSS 443 и TCP 6667) работают нативными клиентами.")

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
