#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
twitch_connect_test.py — независимая проверка соединения с Twitch IRC.

Диагностирует проблему «сообщения перестают появляться через ~60-120 секунд»:
  1. DNS      — резолвинг irc.chat.twitch.tv
  2. TCP      — подключение к порту 6667 (то же, что делает dogs_chat.py)
  3. TLS/WSS  — подключение к irc-ws.chat.twitch.tv:443 (чистый Python, ssl+websocket,
                БЕЗ сторонних пакетов — работает даже при externally-managed pip)
  4. IRC      — CAP/JOIN как dogs_chat.py, счёт PRIVMSG по минутам
  5. KEEPALIVE— PING каждые 30 c + контроль ответов сервера
  6. WATCHDOG— детект half-open: 125 c без единого байта = сокет мёртв

Скрипт НЕ зависит от dogs_chat.py и ничего не чинит — только измеряет.
Запускайте на сервере (OBS-машине), где работает dogs-chat.

Использование:
    python3 tests/twitch_connect_test.py                      # анонимно, 180 c
    python3 tests/twitch_connect_test.py --channel mamilokuchii --duration 300
    python3 tests/twitch_connect_test.py --config /opt/dogs-chat/config/settings.json
                                                            # возьмёт канал/токен из настроек
    python3 tests/twitch_connect_test.py --token oauth:xxxx --nick мой_ник

Выход: 0 = всё ОК; 1 = соединение умирало/не отвечало; 2 = не удалось подключиться.
"""
import argparse
import json
import os
import socket
import ssl
import sys
import time
from collections import Counter

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6667
WSS_HOST = "irc-ws.chat.twitch.tv"
WSS_PORT = 443
READ_TIMEOUT = 1.0        # так же, как в dogs_chat.RawTCPClient
PING_EVERY = 30.0         # наш PING каждые 30 c
DEAD_AFTER = 125.0        # столько без ЕДИНОГО байта => half-open (как watchdog)


def now():
    return time.monotonic()


def ts():
    return time.strftime("%H:%M:%S")


def log(msg):
    print(f"[{ts()}] {msg}", flush=True)


def parse_irc_line(line):
    """Минимальный парсер IRCv3 (как в dogs_chat.py)."""
    tags = {}
    if line.startswith("@"):
        tagpart, _, line = line.partition(" ")
        for kv in tagpart[1:].split(";"):
            k, _, v = kv.partition("=")
            tags[k] = v.replace("\\s", " ").replace("\\:", ";").replace("\\r", "\r").replace("\\n", "\n").replace("\\\\", "\\")
        line = line.strip()
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line.partition(" ")
        prefix = prefix[1:]
    params = []
    rest = line
    while rest:
        if rest.startswith(":"):
            params.append(rest[1:])
            break
        head, sep, rest = rest.partition(" ")
        if head:
            params.append(head)
        if not sep:
            break
    cmd = params[0].upper() if params else ""
    return {"tags": tags, "prefix": prefix, "command": cmd, "params": params}


class WssClient:
    """Минимальный WebSocket-клиент (RFC 6455) поверх стандартного ssl-сокета.
    Никаких сторонних пакетов не требует — работает даже там, где pip заблокирован
    (externally-managed-environment / PEP 668). Twitch отдаёт только текстовые кадры;
    бинарные/фрагментированные тоже поддерживаем, ping/pong/ping-response обрабатываем."""

    OPCODES_CONT, OPCODES_TEXT, OPCODES_BIN = 0x0, 0x1, 0x2
    OPCODES_CLOSE, OPCODES_PING, OPCODES_PONG = 0x8, 0x9, 0xA

    def __init__(self, host, port=WSS_PORT, timeout=10):
        import base64
        import hashlib
        import os as _os

        ctx = ssl.create_default_context()
        raw = socket.create_connection((host, port), timeout=timeout)
        self.sock = ctx.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(_os.urandom(16)).decode()
        req = (f"GET / HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\nOrigin: https://twitch.tv\r\n\r\n")
        self.sock.sendall(req.encode())
        # читаем заголовок handshake по байтово (он маленький)
        hdr = b""
        while not hdr.endswith(b"\r\n\r\n"):
            b = self.sock.recv(1)
            if not b:
                raise ConnectionError("handshake: сервер закрыл соединение")
            hdr += b
        expect = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        status = hdr.split(b"\r\n", 1)[0].decode(errors="replace")
        low = hdr.lower()
        if b"101" not in status.encode() or expect.encode().lower() not in low:
            raise ConnectionError(f"handshake отклонён: {status!r}")
        self._rxbuf = b""       # сырые байты из сокета
        self._payload = b""     # собранный незавершённого фрагмента
        self._frag_op = None
        self.closed = False

    # --- низкоуровневое чтение кадров ---
    def _read_exact(self, n, deadline):
        while len(self._rxbuf) < n:
            remaining = deadline - now()
            if remaining <= 0:
                raise socket.timeout()
            self.sock.settimeout(min(remaining, 1.0))
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                if now() >= deadline:
                    raise
                continue
            if not chunk:
                raise ConnectionError("сервер закрыл соединение (0 байт)")
            self._rxbuf += chunk
        out, self._rxbuf = self._rxbuf[:n], self._rxbuf[n:]
        return out

    def _send_frame(self, opcode, payload=b""):
        import os as _os
        mask = _os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def _next_message(self, deadline):
        """Возвращает следующий текстовый кадр (bytes); служебные кадры обрабатывает сама."""
        while True:
            b0, b1 = self._read_exact(2, deadline)
            fin = b0 & 0x80
            op = b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = int.from_bytes(self._read_exact(2, deadline), "big")
            elif ln == 127:
                ln = int.from_bytes(self._read_exact(8, deadline), "big")
            payload = self._read_exact(ln, deadline) if ln else b""
            if op == self.OPCODES_PING:
                self._send_frame(self.OPCODES_PONG, payload)
                continue
            if op == self.OPCODES_PONG:
                continue
            if op == self.OPCODES_CLOSE:
                self.closed = True
                raise ConnectionError("сервер прислал WS CLOSE")
            if op in (self.OPCODES_TEXT, self.OPCODES_BIN):
                if fin:
                    return payload
                self._payload = payload
                self._frag_op = op
                continue
            if op == self.OPCODES_CONT:
                self._payload += payload
                if fin:
                    out, self._payload = self._payload, b""
                    return out
                continue

    def send(self, text):
        self._send_frame(self.OPCODES_TEXT, text.encode("utf-8", "replace"))

    def recv_lines(self, timeout=READ_TIMEOUT):
        """Как у Conn: возвращает ВСЕ готовые строки до истечения timeout."""
        deadline = now() + timeout
        lines = []
        while True:
            remaining = deadline - now()
            if remaining <= 0:
                return lines
            try:
                chunk = self._next_message(deadline)
            except (socket.timeout, TimeoutError):
                return lines
            except OSError as e:
                raise ConnectionError(f"ws recv: {type(e).__name__}: {e}")
            self._payload_buf = getattr(self, "_payload_buf", b"") + chunk
            while b"\r\n" in self._payload_buf:
                raw, _, self._payload_buf = self._payload_buf.partition(b"\r\n")
                if raw:
                    lines.append(raw.decode("utf-8", "replace"))

    def close(self):
        try:
            self._send_frame(self.OPCODES_CLOSE)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class Conn:
    """Обёртка: либо голый TCP-сокет, либо WssClient (WebSocket без внешних зависимостей)."""

    def __init__(self, sock=None, ws=None, label=""):
        self.sock = sock
        self.ws = ws
        self.label = label
        self.buf = b""

    def send(self, data):
        if self.sock is not None:
            self.sock.sendall(data.encode("utf-8", "replace"))
        else:
            self.ws.send(data)

    def recv_lines(self, timeout=READ_TIMEOUT):
        """Читает ВЕСЬ доступный трафик до таймаута (как исправленный dogs_chat).
        Возвращает список строк; пустой список = тишина timeout секунд."""
        if self.ws is not None:
            return self.ws.recv_lines(timeout)
        deadline = now() + timeout
        lines = []
        while True:
            remaining = deadline - now()
            if remaining <= 0:
                return lines
            try:
                self.sock.settimeout(remaining)
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return lines
            except OSError as e:
                raise ConnectionError(f"recv: {type(e).__name__}: {e}")
            if not chunk:
                raise ConnectionError("recv: сервер закрыл соединение (0 байт)")
            self.buf += chunk
            while b"\r\n" in self.buf:
                raw, _, self.buf = self.buf.partition(b"\r\n")
                if raw:
                    lines.append(raw.decode("utf-8", "replace"))


def step_dns():
    log("== ШАГ 1: DNS ==")
    try:
        infos = socket.getaddrinfo(IRC_HOST, IRC_PORT, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        log(f"OK: {IRC_HOST} -> {', '.join(ips)}")
        return True
    except OSError as e:
        log(f"FAIL: резолвинг не удался: {e}")
        log("=> Проблема в DNS сети сервера. Проверьте /etc/resolv.conf, TryNextServer.")
        return False


def step_tcp():
    log(f"== ШАГ 2: TCP-подключение к {IRC_HOST}:{IRC_PORT} ==")
    try:
        s = socket.create_connection((IRC_HOST, IRC_PORT), timeout=10)
        peer = s.getpeername()
        s.close()
        log(f"OK: порт 6667 открыт ({peer[0]}:{peer[1]})")
        return True
    except OSError as e:
        log(f"FAIL: {type(e).__name__}: {e}")
        log("=> Порт 6667 блокируется фаерволом/провайдером. Используйте WSS 443 (--wss).")
        return False


def step_wss():
    log(f"== ШАГ 2b: TLS/WSS к {WSS_HOST}:{WSS_PORT} ==")
    try:
        ctx = ssl.create_default_context()
        s = socket.create_connection((WSS_HOST, WSS_PORT), timeout=10)
        tls = ctx.wrap_socket(s, server_hostname=WSS_HOST)
        tls.close()
        log("OK: TLS-handshake на 443 прошёл (websockets-пакет НЕ нужен)")
        return True
    except OSError as e:
        log(f"FAIL: {type(e).__name__}: {e}")
        return False


def run_irc(conn, channel, nick, token, duration):
    """Основной прогон: регистрация, JOIN, чтение, PING, статистика по минутам."""
    t0 = now()
    conn.send("CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
    if token:
        tok = token if token.startswith("oauth:") else f"oauth:{token}"
        conn.send(f"PASS {tok}\r\n")
        conn.send(f"NICK {nick}\r\n")
        log(f"Авторизация: NICK={nick} PASS=oauth:***")
    else:
        anon = f"justinfan{int(time.time()) % 99999}"
        conn.send(f"NICK {anon}\r\n")
        nick = anon
        log(f"Анонимный вход: NICK={nick}")
    conn.send(f"JOIN #{channel}\r\n")

    cmds = Counter()
    privmsg_total = 0
    privmsg_in_minute = 0
    bytes_in = 0
    last_any_rx = now()
    last_ping_sent = now()
    pong_from_server = 0     # ответы НА наши PING
    ping_from_server = 0     # серверные PING (мы отвечаем PONG)
    our_pongs = 0
    dead = False

    log(f"-- старт чтения на {duration:.0f} c (канал #{channel}) --")
    while now() - t0 < duration:
        try:
            lines = conn.recv_lines()
        except ConnectionError as e:
            log(f"!! СОКЕТ УМЕР: {e}")
            dead = True
            break

        if lines:
            gap = now() - last_any_rx
            if gap > 30:
                log(f"(пауза входящего трафика была {gap:.0f} c)")
            last_any_rx = now()
            bytes_in += sum(len(l) + 2 for l in lines)

        for line in lines:
            p = parse_irc_line(line)
            cmd = p["command"]
            cmds[cmd] += 1
            if cmd == "PRIVMSG":
                privmsg_total += 1
                privmsg_in_minute += 1
                user = (p["tags"].get("display-name") or
                        (p["prefix"].split("!")[0] if p["prefix"] else "?"))
                text = (p["params"][1] if len(p["params"]) > 1 else "")[:60]
                log(f"   msg: {user}: {text}")
            elif cmd == "PING":
                ping_from_server += 1
                conn.send("PONG :tmi.twitch.tv\r\n")
                our_pongs += 1
            elif cmd == "PONG":
                pong_from_server += 1
            elif cmd in ("JOIN", "366", "001"):
                log(f"   irc< {line[:80]}")

        # наш keepalive-PING каждые PING_EVERY
        if now() - last_ping_sent >= PING_EVERY:
            try:
                conn.send(f"PING :keepalive{int(now())}\r\n")
                last_ping_sent = now()
            except OSError as e:
                log(f"!! не удалось отправить PING: {e}")
                dead = True
                break

        # watchdog: ни одного байта DEAD_AFTER секунд
        if now() - last_any_rx >= DEAD_AFTER:
            log(f"!!! WATCHDOG: {DEAD_AFTER:.0f} c НИ ОДНОГО байта от сервера, "
                f"наши PING без ответа => half-open (NAT/провайдер уронил путь).")
            dead = True
            break

        # поминутная сводка
        mins = int((now() - t0) // 60)
        mark = getattr(run_irc, "_mark", 0)
        if mins > mark:
            run_irc._mark = mins
            log(f"--- [{mins} мин] PRIVMSG за минуту: {privmsg_in_minute}, всего: {privmsg_total}; "
                f"байт: {bytes_in}; PING от сервера: {ping_from_server}, PONG в ответ: {our_pongs}, "
                f"ответов на наши PING: {pong_from_server}; "
                f"тишина сейчас: {now() - last_any_rx:.0f} c ---")
            privmsg_in_minute = 0

    total = now() - t0
    log("== ИТОГ ПРОГОНА ==")
    log(f"Длительность: {total:.0f} c; сообщений PRIVMSG: {privmsg_total}; байт принято: {bytes_in}")
    log(f"Команды от сервера: {dict(cmds.most_common())}")
    log(f"Серверных PING: {ping_from_server}, наших PONG: {our_pongs}, ответов на наши PING: {pong_from_server}")
    if dead:
        log("ВЫВОД: соединение УМИРАЛО во время прогона (см. строку '!!!' выше). "
            "Если это повторяется стабильно на одном и том же времени — виноват NAT/фаервол сети сервера.")
    elif privmsg_total == 0 and total > 120:
        log("ВЫВОД: соединение ЖИВОЕ (данные ходят/PING отвечают), но PRIVMSG 0 — "
            "либо в чате реально тихо, либо проблема в канале доставки dogs-chat (SSE/OBS), а не в IRC.")
    else:
        log("ВЫВОД: соединение ЗДОРОВО — сообщения шли весь прогон без переподключений.")
    return dead, privmsg_total


def main():
    ap = argparse.ArgumentParser(description="Диагностика соединения с Twitch IRC")
    ap.add_argument("--channel", default="", help="канал без # (по умолчанию — из --config или mamilokuchii)")
    ap.add_argument("--duration", type=float, default=180, help="сколько секунд читать чат (по умолчанию 180)")
    ap.add_argument("--config", default="/opt/dogs-chat/config/settings.json",
                    help="путь к settings.json dogs-chat (для канала/токена)")
    ap.add_argument("--nick", default="", help="nickname для авторизации")
    ap.add_argument("--token", default="", help="oauth-токен (можно без префикса oauth:)")
    ap.add_argument("--wss", action="store_true", help="использовать WebSocket 443 вместо TCP 6667")
    args = ap.parse_args()

    channel, nick, token = args.channel, args.nick, args.token
    if os.path.exists(args.config):
        try:
            cfg = json.load(open(args.config, encoding="utf-8"))
            channel = channel or (cfg.get("channels") or [""])[0]
            token = token or cfg.get("oauth_token", "")
            nick = nick or cfg.get("nickname", "")
            log(f"Настройки взяты из {args.config}: канал=#{channel}, "
                f"токен={'есть' if token else 'нет (анонимно justinfan)'}")
        except Exception as e:
            log(f"config не прочитан ({e}); использую аргументы CLI")
    channel = channel or "mamilokuchii"

    log(f"Тест соединения с Twitch IRC | канал=#{channel} | транспорт={'WSS 443' if args.wss else 'TCP 6667'} | {args.duration:.0f} c")
    ok_dns = step_dns()
    if not ok_dns:
        return 2
    if args.wss:
        ok_port = step_wss()
    else:
        ok_port = step_tcp()
    if ok_port is False:
        return 2

    exit_code = 0
    try:
        if args.wss:
            ws = WssClient(WSS_HOST, WSS_PORT)
            conn = Conn(ws=ws, label="WSS 443")
            log("Подключено через WebSocket 443 (чистый Python, без pip-пакетов)")
        else:
            s = socket.create_connection((IRC_HOST, IRC_PORT), timeout=10)
            conn = Conn(sock=s, label="TCP 6667")
            log("Подключено через TCP 6667")
    except (OSError, ConnectionError) as e:
        log(f"FAIL: не удалось подключиться: {type(e).__name__}: {e}")
        return 2

    try:
        dead, _ = run_irc(conn, channel, nick, token, args.duration)
        exit_code = 1 if dead else 0
    finally:
        try:
            if conn.sock is not None:
                conn.sock.close()
            elif conn.ws is not None:
                conn.ws.close()
        except Exception:
            pass

    log("Готово." + (" Соединение было нестабильно — см. ВЫВОД выше." if exit_code == 1 else " Стабильно."))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
