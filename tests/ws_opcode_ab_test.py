#!/usr/bin/env python3
"""A/B-тест: BINARY vs TEXT исходящие WS-кадры против ЖИВОГО Twitch (порт 443).

Доказывает корень бага «WS: сервер закрыл соединение (0 байт) сразу после
JOIN»: Twitch молча рвёт WebSocket, если клиент шлёт IRC-команды BINARY-
кадрами (opcode 0x2), и держит соединение, если теми же командами как
TEXT (opcode 0x1).

Запуск:  python3 tests/ws_opcode_ab_test.py [--channel jynxzi] [--seconds 25]
Коды выхода: 0 = гипотеза подтверждена (BIN умирает, TEXT живёт).
"""
import argparse
import base64
import os
import socket
import ssl
import struct
import sys
import time

HOST = "irc-ws.chat.twitch.tv"
PORT = 443


def ws_connect():
    key = base64.b64encode(os.urandom(16)).decode()
    ctx = ssl.create_default_context()
    raw = socket.create_connection((HOST, PORT), timeout=15)
    sock = ctx.wrap_socket(raw, server_hostname=HOST)
    req = (f"GET / HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
           f"Sec-WebSocket-Version: 13\r\nOrigin: https://twitch.tv\r\n\r\n")
    sock.sendall(req.encode())
    hdr = b""
    while not hdr.endswith(b"\r\n\r\n"):
        b = sock.recv(1)
        if not b:
            raise ConnectionError("закрыт на handshake")
        hdr += b
    if b"101" not in hdr.split(b"\r\n", 1)[0]:
        raise ConnectionError(f"handshake отклонён: {hdr[:120]!r}")
    return sock


def _recv_n(sock, n):
    out = b""
    while len(out) < n:
        c = sock.recv(n - len(out))
        if not c:
            raise ConnectionError("обрыв посреди кадра")
        out += c
    return out


def send_frame(sock, opcode, payload):
    mask = os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
    else:
        head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(head + mask + masked)


def read_case(sock, deadline):
    """Читать кадры до дедлайна. Возвращает dict: lines, privmsg, died,
    died_after_s, first_line."""
    buf = b""
    st = {"lines": 0, "privmsg": 0, "died": False, "died_after_s": None,
          "first_line": ""}
    t0 = time.monotonic()
    while time.monotonic() < deadline:
        try:
            b0 = _recv_n(sock, 1)
        except ConnectionError:
            st["died"] = True
            st["died_after_s"] = time.monotonic() - t0
            return st
        except (socket.timeout, TimeoutError):
            continue
        try:
            b1 = _recv_n(sock, 1)
        except ConnectionError:
            st["died"] = True
            st["died_after_s"] = time.monotonic() - t0
            return st
        op = b0[0] & 0x0F
        ln = b1[0] & 0x7F
        try:
            if ln == 126:
                ln = struct.unpack("!H", _recv_n(sock, 2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", _recv_n(sock, 8))[0]
            payload = _recv_n(sock, ln) if ln else b""
        except ConnectionError:
            st["died"] = True
            st["died_after_s"] = time.monotonic() - t0
            return st
        if op in (0x1, 0x2):
            buf += payload
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                line = line.strip()
                if not line:
                    continue
                st["lines"] += 1
                if not st["first_line"]:
                    st["first_line"] = line.decode("utf-8", "replace")[:80]
                if b" PRIVMSG " in line:
                    st["privmsg"] += 1
    return st


def run_case(opcode, channel, seconds, label):
    import random
    sock = ws_connect()
    # НICK должен быть валидным IRC-ником: только буквы/цифры/_ (без «BIN»-хвоста
    # с заглавными и без лишних символов) — иначе Twitch отвечает 421 Unknown
    # command и JOIN не происходит.
    # НICK должен идти ПЕРСЫМ кадром (до CAP REQ): Twitch закрывает соединение,
    # если первым приходит не NICK/PASS.
    nick = "justinfan%d" % random.randint(100000, 999999)
    cmds = [f"NICK {nick}",
            "CAP REQ :twitch.tv/tags twitch.tv/commands",
            f"JOIN #{channel}"]
    payload = "\r\n".join(cmds).encode()
    # КЛЮЧЕВОЙ момент теста: НICK уходит ОТДЕЛЬНЫМ первым кадром. Twitch
    # требует регистрацию до любых других команд: если NICK пачкой с CAP REQ
    # в одном кадре, сервер отвечает NOTICE "Invalid NICK" и рвёт соединение
    # независимо от опкода (эту ошибку ловили на первых прогонах теста).
    # Далее: BIN — как делал старый баг (send(bytes)->OP_BIN), TEXT — как
    # исправленный код dogs_chat.py (send(str)->OP_TEXT).
    send_frame(sock, opcode, cmds[0].encode())
    time.sleep(0.4)
    send_frame(sock, opcode, "\r\n".join(cmds[1:]).encode())
    st = read_case(sock, time.monotonic() + seconds)
    try:
        sock.close()
    except Exception:
        pass
    kind = "TEXT" if opcode == 0x1 else "BINARY"
    print(f"[{kind}] строк получено: {st['lines']} (PRIVMSG: {st['privmsg']}); "
          f"умерло: {'ДА через %.1f c' % st['died_after_s'] if st['died'] else 'НЕТ — живо все %d c' % seconds}; "
          f"первая строка: {st['first_line'] or '-'}")
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="jynxzi")
    ap.add_argument("--seconds", type=int, default=25)
    a = ap.parse_args()
    print(f"A/B тест WS-опкодов против {HOST}:{PORT}, канал #{a.channel}, "
          f"{a.seconds} c на случай\n")
    bin_st = run_case(0x2, a.channel, a.seconds, "BIN")
    time.sleep(5)   # пауза между кейсами — не провоцировать rate-limit
    txt_st = run_case(0x1, a.channel, a.seconds, "TXT")
    print()
    if bin_st["died"] and not txt_st["died"] and txt_st["lines"] > 0:
        print("ВЫВОД: гипотеза ПОДТВЕРЖДЕНА — BINARY-кадры Twitch рвёт сразу, "
              "TEXT-кадры живут и получают трафик чата весь прогон. Исправление "
              "в dogs_chat.py (str -> TEXT-кадр) корректно.")
        return 0
    print("ВЫВОД: паттерн не воспроизвёлся в этом прогоне (возможны rate-limit "
          "по IP или сетевые помехи) — повторите тест через 2-3 минуты.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
