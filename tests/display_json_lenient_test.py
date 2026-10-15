#!/usr/bin/env python3
"""Тест: display.json прощает типичные ручные правки (висячая запятая, //комменты, «ёлочки»).

Раньше из-за одной лишней запятой сервер молча держал старый конфиг и
«правки не применялись». Теперь такие файлы чинятся автоматически.

Запуск:  python3 tests/display_json_lenient_test.py
"""
import sys, os, json, tempfile, pathlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dogs_chat as dc

fails = []

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

# юнит-тест _json_lenient
good = '{"dog_scale": 1.5}'
check("strict json", dc._json_lenient(good) == {"dog_scale": 1.5})
check("trailing comma", dc._json_lenient('{"dog_scale": 1.5,}')["dog_scale"] == 1.5)
check("comment", dc._json_lenient('{"dog_scale": 1.5  // размер\n}')["dog_scale"] == 1.5)
check("quotes-yolochki", dc._json_lenient('{“dog_scale”: 1.5}')["dog_scale"] == 1.5)
try:
    dc._json_lenient("{это вообще не json")
    check("broken still raises", False)
except Exception:
    check("broken still raises", True)

# сквозной тест через load_display_config с реальным файлом
with tempfile.TemporaryDirectory() as td:
    p = pathlib.Path(td) / "display.json"
    dc.DISPLAY_CONFIG_PATH = p
    dc._display_cache.update({"mtime": None, "cfg": None})
    p.write_text('{"name_font_px": 22,\n "dog_scale": 1.8,\n}', encoding="utf-8")
    cfg = dc.load_display_config()
    check("file with trailing comma applied", cfg["name_font_px"] == 22 and cfg["dog_scale"] == 1.8)
    # битый файл -> держим последний исправный, а не дефолты
    p.write_text('{"name_font_px": ???', encoding="utf-8")
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    dc._display_cache["mtime"] = None  # форсируем перечитку
    cfg2 = dc.load_display_config()
    check("broken file keeps last good config", cfg2["name_font_px"] == 22)
    # починили -> новые значения применяются
    p.write_text('{"name_font_px": 30,}', encoding="utf-8")
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    dc._display_cache["mtime"] = None
    cfg3 = dc.load_display_config()
    check("fixed file applies new value", cfg3["name_font_px"] == 30)

print("\nИТОГ:", "PASSED" if not fails else f"FAILED: {fails}")
sys.exit(0 if not fails else 1)
