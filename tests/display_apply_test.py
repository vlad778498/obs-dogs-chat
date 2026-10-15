#!/usr/bin/env python3
"""Тест: конфиг отображения ПРИМЕНЯЕТСЯ в UI корректно (без «съедания» нулей).

Предыстория бага: «display.json перестал применяться». Сервер отдавал
/api/config правильно, но UI терял значения из-за паттернов вида
`x || дефолт` (законный 0 подменялся дефолтом) и Math.max(0.2, x) без
проверки NaN. Здесь проверяем:
  1) серверный /api/config реально отдаёт значения из display.json;
  2) в ui/dogs.js больше нет опасных паттернов для ключевых полей;
  3) sanitize-функция num() в коде присутствует и используется.

Запуск:  python3 tests/display_apply_test.py
"""
import sys, os, json, tempfile, pathlib, re

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import dogs_chat as dc

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

# ---------------------------------------------------------------- server side
with tempfile.TemporaryDirectory() as td:
    p = pathlib.Path(td) / "display.json"
    p.write_text(json.dumps({
        "dog_scale": 0.8, "kennel_scale": 0.5, "name_font_px": 24,
        "ground_offset_px": 0, "no_dog_zone_pct": {"x":35,"y":0,"w":30,"h":25},
    }), encoding="utf-8")
    dc.DISPLAY_CONFIG_PATH = p
    dc._display_cache["mtime"] = None
    dc._display_cache["cfg"] = None
    cfg = dc.load_display_config()
    check("server: dog_scale доходит", cfg.get("dog_scale") == 0.8)
    check("server: kennel_scale доходит", cfg.get("kennel_scale") == 0.5)
    check("server: name_font_px доходит", cfg.get("name_font_px") == 24)
    check("server: ground_offset 0 не превращается в None",
          cfg.get("ground_offset_px") == 0)
    check("server: no_dog_zone доходит",
          isinstance(cfg.get("no_dog_zone_pct"), dict) and
          cfg["no_dog_zone_pct"].get("w") == 30)

    # изменение файла -> перечитка (по mtime+размеру)
    import time; time.sleep(0.01)
    p.write_text(json.dumps({"dog_scale": 1.7}), encoding="utf-8")
    os.utime(p, (p.stat().st_atime + 5, p.stat().st_mtime + 5))
    cfg2 = dc.load_display_config()
    check("server: live-reload работает", cfg2.get("dog_scale") == 1.7)

# ------------------------------------------------------------------ client JS
js = (BASE / "ui" / "dogs.js").read_text(encoding="utf-8")

check("js: есть sanitize-хелпер num()", "const num = (v, def, lo, hi)" in js or
      re.search(r"function\s+num\s*\(", js) is not None)
check("js: dog_scale проходит через num()",
      re.search(r"DisplayCfg\.dog_scale\s*=\s*num\(", js) is not None)
check("js: kennel_scale проходит через num()",
      re.search(r"DisplayCfg\.kennel_scale\s*=\s*num\(", js) is not None)
check("js: name_font_px проходит через num()",
      re.search(r"DisplayCfg\.name_font_px\s*=\s*num\(", js) is not None)

# опасные паттерны «|| дефолт» для этих полей должны исчезнуть
bad = re.findall(r"(dog_scale|kennel_scale|name_font_px)\s*\|\|", js)
check("js: нет паттерна `поле || дефолт` (съедает законный 0)", not bad)

# updateCrowdScale тоже должен использовать finite-safe вариант
m = re.search(r"let target = ([^;]+);", js[js.index("function updateCrowdScale"):])
check("js: updateCrowdScale не использует `c.dog_scale || 1`",
      m is not None and "||" not in m.group(1))

# ретрай загрузки конфига при сетевой ошибке
check("js: fetchDisplayCfg делает ретрай при ошибке",
      "setTimeout(fetchDisplayCfg" in js)

print()
if fails:
    print("FAILED:", len(fails)); sys.exit(1)
print("DISPLAY APPLY TEST PASSED")
