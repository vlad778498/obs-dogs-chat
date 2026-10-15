#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тест запретной зоны (no_dog_zone_pct) для ui/dogs.js.

Что проверяет:
 1. Синтаксис JS (node --check), если node доступен;
 2. config/display.example.json валиден и содержит новые ключи;
 3. dogs_chat.py: DEFAULT_DISPLAY_CONFIG содержит no_dog_zone_pct/show,
    /api/config отдаёт их по HTTP (реальный сервер на случайном порту);
 4. ui/index.html содержит <div id="nodogzone">, ui/dogs.css — стили #nodogzone;
 5. Логическая модель алгоритма (порт ключевых функций JS на Python):
      - freeSpot не возвращает точку внутри зоны, если собака с ней
        пересекается по вертикали;
      - zoneEscapeX всегда даёт точку вне зоны (оба края, затем lo);
      - собака, «застрявшая» в зоне, после выдворения+расталкивания вне её;
      - собака выше зоны (y/h не пересекаются) НЕ выдворяется;
      - при no_dog_zone_pct=null всё поведение идентично старому.

Запуск:  python3 tests/no_dog_zone_test.py
Код выхода: 0 = PASS, 1 = FAIL.
"""
import json
import re
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- model ----
# Порт геометрии из dogs.js: yard W=1000 H=120, dogW=90, собака стоит на полу
W, H, DW = 1000.0, 120.0, 90.0


def make_zone(zpct):
    """Порт updateNoDogZone(): проценты -> пиксели, y от НИЖНЕГО края."""
    if not zpct:
        return None
    z = {k: max(0.0, min(100.0, float(v or 0))) for k, v in zpct.items()}
    zx, zw = z["x"] / 100 * W, z["w"] / 100 * W
    zh, zy = z["h"] / 100 * H, z["y"] / 100 * H
    return {"x": zx, "w": zw, "h": zh, "yTop": H - zy - zh}


def dog_in_zone(x, z, dog_bottom=0.0, dog_h=DW):
    """Порт dogInZone() для собаки на полу (bottom=0)."""
    if not z:
        return False
    t, b = H - dog_bottom - dog_h, H - dog_bottom
    return (x + DW / 2 > z["x"]) and (x - DW / 2 < z["x"] + z["w"]) \
        and (b > z["yTop"]) and (t < z["yTop"] + z["h"])


def zone_escape_x(x, z, m=4.0):
    """Порт zoneEscapeX(d)."""
    left_free = z["x"] - DW / 2 - 6
    right_free = z["x"] + z["w"] + DW / 2 + 6
    lo, hi = m + DW / 2, W - m - DW / 2
    cand = [c for c in (left_free, right_free) if lo <= c <= hi]
    if not cand:
        return lo
    return min(cand, key=lambda c: abs(c - x))


def subtract_ivs(ivs, a, b):
    out = []
    for x, y in ivs:
        if b <= x or a >= y:
            out.append((x, y)); continue
        if a > x:
            out.append((x, min(a, y)))
        if b < y:
            out.append((max(b, x), y))
    return [(x, y) for x, y in out if y > x]


def free_spot(lo, hi, reserved, z, dpos):
    """Порт freeSpot(): зона вырезается только если собака уже в ней по вертикали."""
    if hi <= lo:
        return lo
    ivs = [(lo, hi)]
    allr = list(reserved)
    if z and dog_in_zone(dpos, z):
        allr.append((z["x"] - DW / 2 - 6, z["x"] + z["w"] + DW / 2 + 6))
    for a, b in allr:
        ivs = subtract_ivs(ivs, a, b)
        if not ivs:
            break
    if not ivs:
        return lo
    total = sum(y - x for x, y in ivs)
    r = (total) * 0.999  # детерминированно: берём почти конец последнего интервала
    for x, y in ivs:
        ln = y - x
        if r <= ln:
            return x + r
        r -= ln
    return ivs[-1][1]


def main():
    js = (BASE / "ui" / "dogs.js").read_text(encoding="utf-8")
    css = (BASE / "ui" / "dogs.css").read_text(encoding="utf-8")
    html = (BASE / "ui" / "index.html").read_text(encoding="utf-8")
    ex = json.loads((BASE / "config" / "display.example.json").read_text(encoding="utf-8"))
    py = (BASE / "dogs_chat.py").read_text(encoding="utf-8")

    # 1) синтаксис JS
    try:
        r = subprocess.run(["node", "--check", str(BASE / "ui" / "dogs.js")],
                           capture_output=True, text=True)
        check("node --check ui/dogs.js", r.returncode == 0)
    except FileNotFoundError:
        print("SKIP node --check (node не установлен)")

    # 2) конфиг-пример
    check("example: no_dog_zone_pct в JSON", "no_dog_zone_pct" in ex)
    check("example: no_dog_zone_show в JSON", "no_dog_zone_show" in ex)

    # 3) сервер: /api/config содержит ключи (реальный HTTP-запрос)
    sys.path.insert(0, str(BASE))
    import dogs_chat  # noqa: E402
    cfg = dogs_chat.load_display_config()
    check("DEFAULT: no_dog_zone_pct", "no_dog_zone_pct" in cfg)
    check("DEFAULT: no_dog_zone_show", "no_dog_zone_show" in cfg)

    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dogs_chat.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=5) as resp:
        got = json.loads(resp.read().decode("utf-8"))
    srv.shutdown()
    check("/api/config отдаёт no_dog_zone_pct", "no_dog_zone_pct" in got)

    # 4) разметка/стили
    check('index.html: div id="nodogzone"', 'id="nodogzone"' in html)
    check("dogs.css: селектор #nodogzone", "#nodogzone" in css)
    for fn in ("updateNoDogZone", "dogInZone", "zoneEscapeX", "dogVerticalSpan"):
        check(f"dogs.js: функция {fn}", re.search(r"function %s\b" % fn, js) is not None)
    check("dogs.js: freeSpot вызывается с собакой", "freeSpot(m, Math.max(m + 2, w - dw - m), d)" in js)
    check("dogs.js: tickWalk выгоняет из зоны", "zoneEscapeX(d);" in js)

    # 5) логическая модель
    z = make_zone({"x": 35, "y": 0, "w": 30, "h": 25})   # плашка по центру внизу
    check("зона: x=35%..65% ширины", abs(z["x"] - 350) < 1e-6 and abs(z["x"] + z["w"] - 650) < 1e-6)
    check("зона: h=25% высоты над полом", abs(z["yTop"] - (H - 30)) < 1e-6)
    check("собака под плашкой считается в зоне", dog_in_zone(500, z))
    check("собака слева от плашки — вне зоны", not dog_in_zone(100, z))

    esc = zone_escape_x(500, z)
    check("выдворение: точка вне зоны", not dog_in_zone(esc, z))
    check("выдворение: ближайший край (305)", abs(esc - (350 - DW / 2 - 6)) < 1e-6)

    # цель внутри зоны невозможна, если собака в ней
    spot = free_spot(49, W - DW - 49, [], z, 500)
    check("freeSpot: цель не внутри зоны", not (z["x"] < spot < z["x"] + z["w"]))
    # собака вне зоны по горизонтали — зона её не касается
    spot2 = free_spot(49, W - DW - 49, [], z, 100)
    check("freeSpot: вне зоны свободный выбор", 49 <= spot2 <= W - DW - 49)
    # высокая узкая плашка над головой собаки (y=80%..) — не мешает
    z_hi = make_zone({"x": 30, "y": 80, "w": 40, "h": 15})
    check("плашка у потолка игнорируется собакой на полу", not dog_in_zone(500, z_hi))
    # null-зона = прежнее поведение
    check("зона null: dog_in_zone=False", not dog_in_zone(500, None))
    spot3 = free_spot(49, W - DW - 49, [], None, 500)
    check("зона null: freeSpot как раньше", 49 <= spot3 <= W - DW - 49)

    print("\n%d ошибок" % len(FAILS))
    if FAILS:
        for f in FAILS:
            print(" -", f)
    print("NO-DOG-ZONE TEST: " + ("PASSED" if not FAILS else "FAILED"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
