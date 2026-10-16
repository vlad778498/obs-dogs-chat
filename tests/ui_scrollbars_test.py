#!/usr/bin/env python3
"""Тест: страница dogs-chat не создаёт переполнение (полосы прокрутки в OBS).

Проверяет два уровня защиты от полос прокрутки в OBS Browser Source:
1) CSS-уровень: html/body overflow:hidden, #scene без 100vw/100vh,
   #yard с overflow:hidden и увеличенной на 24px верхней зоной (чтобы ник
   над головой собаки не обрезался);
2) JS-уровень: clampDog()/nameOverhang() держат собаку+ник внутри экрана,
   pickTarget/tickWalk/addWalker их вызывают, обработчик resize есть.

Запуск:  python3 tests/ui_scrollbars_test.py [--serve]
  --serve — дополнительно поднять HTTP-сервер из dogs_chat.py на время теста.
Без зависимостей, только stdlib.
"""
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "ui" / "dogs.css").read_text(encoding="utf-8")
JS = (ROOT / "ui" / "dogs.js").read_text(encoding="utf-8")

checks = []
def check(name, ok, detail=""):
    checks.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + ((" — " + detail) if detail and not ok else ""))

# --- CSS: body/html не должны порождать скролл -----------------------------
m = re.search(r"html,\s*body\s*\{([^}]*)\}", CSS)
check("css: html,body найдено", bool(m))
if m:
    block = m.group(1)
    check("css: html,body overflow:hidden", "overflow: hidden" in block or "overflow:hidden" in block,
          f"в блоке: {block.strip()[:120]}")

check("css: #scene без 100vw/100vh (источник горизонтального скролла)",
      bool(re.search(r"#scene\s*\{[^}]*inset:\s*0", CSS)) and
      not re.search(r"#scene\s*\{[^}]*100v[wh]", CSS))

m = re.search(r"#yard\s*\{([^}]*)\}", CSS)
check("css: #yard найдено", bool(m))
if m:
    yard = m.group(1)
    check("css: #yard overflow:hidden (уходящая собака не растягивает страницу)",
          "overflow: hidden" in yard or "overflow:hidden" in yard)
    check("css: #yard расширен вверх на 24px (ник над головой не обрезается)",
          "-24px" in yard or "24px" in yard)

# --- JS: геометрический контроль -------------------------------------------
for fn in ("function nameOverhang", "function clampDog", "function reposition"):
    check(f"js: {fn}() определена", fn in JS)

check("js: tickWalk вызывает clampDog",
      bool(re.search(r"function tickWalk[\s\S]{0,2600}?clampDog\(d\)", JS)))
check("js: pickTarget учитывает свес ника (m=nameOverhang)",
      bool(re.search(r"function pickTarget[\s\S]{0,1300}?nameOverhang\(d\)", JS)))
check("js: addWalker вызывает clampDog",
      bool(re.search(r"function addWalker[\s\S]{0,1500}?clampDog\(d\)", JS)))
check("js: смена ника обновляет границы (update->clampDog)",
      bool(re.search(r"nm\.textContent !== wantName[\s\S]{0,200}?clampDog\(d\)", JS)))
check("js: обработчик window resize", 'addEventListener("resize"' in JS)

# --- Численная симуляция геометрии -----------------------------------------
# Модель: экран W=1920, собака dw=150, ник nm шириной 300 (длинный) ->
# overhang m=(300-150)/2+4=79. Правый предел x: W-dw-m=1691. Левый: m=79.
W, DW, NM = 1920, 150, 300
M = max(0, (NM - DW) / 2 + 4)
def clamp(x):
    lo, hi = M, max(4, W - DW - M)
    return min(max(x, lo), hi)
worst_right = clamp(W - 10)          # собака прижалась к правому краю
right_edge_of_name = worst_right + DW / 2 + NM / 2   # transform:-50% от центра карточки
worst_left = clamp(-50)
left_edge_of_name = worst_left + DW / 2 - NM / 2
check("geom: длинный ник у правого края НЕ вылезает за экран",
      right_edge_of_name <= W, f"{right_edge_of_name:.0f} <= {W}")
check("geom: длинный ник у левого края НЕ вылезает за экран",
      left_edge_of_name >= 0, f"{left_edge_of_name:.0f} >= 0")

# --- Опционально: живой HTTP-сервер ----------------------------------------
if "--serve" in sys.argv:
    sys.path.insert(0, str(ROOT))
    import dogs_chat as dc
    cfg = dict(dc.DEFAULT_CONFIG) if hasattr(dc, "DEFAULT_CONFIG") else {}
    store = dc.ActivityStore(cfg)
    store.touch("verylongnicknametest", display_name="VeryLongNicknameTest__OBS", color="", is_mod=False)
    dc.Handler.store = store
    dc.Handler.config = cfg
    srv = dc.QuietHTTPServer(("127.0.0.1", 0), dc.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import urllib.request
    for path in ("/", "/static/dogs.css", "/static/dogs.js", "/api/dogs"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                body = r.read()
                cc = r.headers.get("Cache-Control", "")
                check(f"http: {path} отдаётся (200, {len(body)} байт, Cache-Control={cc!r})",
                      r.status == 200 and len(body) > 0)
        except Exception as e:
            check(f"http: {path}", False, repr(e))
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dogs", timeout=5) as r:
        data = __import__("json").loads(r.read())
        names = [d["name"] for d in data["dogs"]]
        check("http: /api/dogs содержит длинный ник", any(len(n) > 20 for n in names), str(names))
    srv.shutdown()

failed = [c for c in checks if not c[1]]
print("-" * 60)
print(f"ИТОГ: {len(checks) - len(failed)}/{len(checks)} проверок пройдено")
sys.exit(1 if failed else 0)
