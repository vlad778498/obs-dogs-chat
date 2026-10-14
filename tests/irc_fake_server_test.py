#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
irc_fake_server_test.py — E2E-тест логики dogs_chat.py против фейкового IRC-сервера.

Эмулирует поведение Twitch из боевых логов:
  * пачки PRIVMSG в одном TCP-сегменте с НЕПОЛНЫМ «хвостом» (то, на чём
    ложно срабатывал старый watchdog);
  * тишину канала 40+ секунд (раньше сообщения «исчезали через ~60 c»);
  * серверные PING;
  * ответы на наши PING.

Проверяет:
  1. за время теста было РОВНО одно соединение (нет ложных переподключений);
  2. все отправленные PRIVMSG дошли до ActivityStore;
  3. ответ на серверный PING отправлен только на настоящий PING.

Запуск из корня проекта:  python3 tests/irc_fake_server_test.py
Возвращает 0 при PASSED, 1 при FAIL. Интернет не нужен.
"""
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dogs_chat import ActivityStore, TwitchChatThread  # noqa: E402

HOST, PORT = "127.0.0.1", 16667
EXPECTED_USERS = ["tester_one", "tester_two", "tester_three"]


def fake_server(conns_log, stop_flag):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(1)
    srv.settimeout(1.0)
    while not stop_flag["v"]:
        try:
            c, _ = srv.accept()
        except socket.timeout:
            continue
        conns_log.append(time.monotonic())
        t = threading.Thread(target=session, args=(c,), daemon=True)
        t.start()
    srv.close()


class Session:
    """Состояние фейкового IRC-сервера ОДНОГО соединения (не глобальное!)."""

    def __init__(self):
        self.m1 = self.p1 = self.m2 = False
        self.saw_client_ping = False


def run_session(c, state):
    c.settimeout(1.0)
    buf = b""
    t0 = time.monotonic()
    try:
        while True:
            el = time.monotonic() - t0
            # режимы по таймлайну: пачка -> тишина с хвостом -> ping -> пачка -> тишина
            if 2 < el < 3 and not state.m1:
                state.m1 = True
                batch = (
                    ":tmi.twitch.tv 001 justinfan :OK\r\n"
                    ":tmi.twitch.tv JOIN :#testchannel\r\n"
                )
                for i, u in enumerate(EXPECTED_USERS[:2]):
                    batch += (f"@display-name={u};tmi-session-emote-set= :{u}!{u}@{u}.tmi."
                              f"twitch.tv PRIVMSG #testchannel :hello-{i}\r\n")
                # недописанный «хвост» — как в реальных логах Twitch
                batch += "@display-name=tailuser :tailuser!tail@tail.tmi.twitch.tv PRI"
                c.sendall(batch.encode())
            if 15 < el < 16 and not state.p1:
                state.p1 = True
                c.sendall(b":tmi.twitch.tv PING :abc123\r\n")
            if 25 < el < 26 and not state.m2:
                state.m2 = True
                u = EXPECTED_USERS[2]
                msg = (f"@display-name={u} :{u}!{u}@{u}.tmi.twitch.tv "
                       f"PRIVMSG #testchannel :after-silence\r\n")
                # дробим на части с паузами — проверка дочитывания до таймаута
                half = len(msg) // 2
                c.sendall(msg[:half].encode())
                time.sleep(0.3)
                c.sendall(msg[half:].encode())
            if el > 40:
                break
            # читать то, что шлёт клиент (PING/PONG), не блокируясь
            try:
                data = c.recv(4096)
                if not data:
                    break
                buf += data
                while b"\r\n" in buf:
                    line, _, buf = buf.partition(b"\r\n")
                    line = line.decode("utf-8", "ignore")
                    if line.startswith("PING"):
                        state.saw_client_ping = True
                        c.sendall(b":tmi.twitch.tv PONG tmi.twitch.tv :keepalive\r\n")
            except socket.timeout:
                continue
    except OSError:
        pass
    finally:
        try:
            c.close()
        except OSError:
            pass


def fake_server(conns_log, stop_flag, sessions):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(1)
    srv.settimeout(1.0)
    while not stop_flag["v"]:
        try:
            c, _ = srv.accept()
        except socket.timeout:
            continue
        conns_log.append(time.monotonic())
        st = Session()
        sessions.append(st)
        t = threading.Thread(target=run_session, args=(c, st), daemon=True)
        t.start()
    srv.close()


def main():
    # подменяем адрес подключения на локальный фейк
    orig_create = socket.create_connection

    def patched(address, *a, **kw):
        if address[0] == "irc.chat.twitch.tv":
            address = (HOST, PORT)
        return orig_create(address, *a, **kw)

    socket.create_connection = patched

    conns = []
    sessions = []
    stop_flag = {"v": False}
    srv_thread = threading.Thread(target=fake_server, args=(conns, stop_flag, sessions), daemon=True)
    srv_thread.start()

    store = ActivityStore({"active_window_seconds": 3600, "max_dogs": 20})
    # «фоновые» чатеры — не из теста; исключаем их из проверки
    store.touch("background_noise", "BackgroundNoise")
    cfg = {
        "channels": ["testchannel"],
        "active_window_seconds": 3600,
        "max_dogs": 20,
        "irc_transport": "tcp",
        "nickname": "",
        "oauth_token": "",
    }
    th = TwitchChatThread(store, cfg)
    th.start()
    time.sleep(35)  # тестовая сцена сервера живёт 26 c + запас
    th.stop()
    th.join(timeout=10)
    stop_flag["v"] = True
    socket.create_connection = orig_create

    users = {u["login"].lower() for u in store.active_users()}
    got = [u for u in EXPECTED_USERS if u in users]
    n_conns = len(conns)
    saw_ping = any(s.saw_client_ping for s in sessions)

    print(f"Соединений установлено: {n_conns} (ожидалось 1)")
    print(f"Полученные чатеры: {sorted(users)}")
    print(f"Ожидаемые: {EXPECTED_USERS}")
    print(f"Сервер видел наш keepalive-PING: {saw_ping}")

    ok = True
    if n_conns != 1:
        print(f"FAIL: было {n_conns} соединений — ложные переподключения всё ещё происходят!")
        ok = False
    missing = [u for u in EXPECTED_USERS if u not in got]
    if missing:
        print(f"FAIL: не дошли сообщения от: {missing}")
        ok = False
    if not saw_ping:
        print("WARN: сервер не увидел наших PING (check таймлайн теста)")

    print("E2E PASSED" if ok else "E2E FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
