#!/usr/bin/env python3
"""AutoFPS · самоперевірка: що налаштовано не так і як це виправити.

check(appid) повертає список пунктів:
  {"id", "level": "ok"|"warn"|"error", "title", "detail", "fix": id виправлення або None}
fix(fix_id) виконує виправлення й повертає (ok, повідомлення).
Модуль не залежить від Decky — його запускають і з тестів:  python3 doctor.py [appid]
"""
import glob
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofps_common as C  # noqa: E402

HOME = os.path.expanduser("~")
LAUNCHER = os.path.join(HOME, ".local", "bin", "autofps-launch")
WRAPPER_LOG = os.path.join(C.CACHE_DIR, "wrapper.log")


def _item(id_, level, title, detail="", fix=None):
    return {"id": id_, "level": level, "title": title, "detail": detail, "fix": fix}


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return ""


def _launch_options(appid):
    """Параметри запуску гри з localconfig.vdf усіх користувачів Steam (перший знайдений)."""
    pat = re.compile(r'^\s*"(\d+)"\s*$')
    for vdf in glob.glob(os.path.join(HOME, ".local/share/Steam/userdata/*/config/localconfig.vdf")):
        try:
            with open(vdf, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            continue
        cur = None
        for line in lines:
            m = pat.match(line)
            if m:
                cur = m.group(1)
                continue
            if cur == str(appid):
                m = re.match(r'\s*"LaunchOptions"\s+"(.*)"\s*$', line)
                if m:
                    return m.group(1)
    return None


def _check_launcher():
    if os.path.isfile(LAUNCHER) and os.access(LAUNCHER, os.X_OK):
        return _item("launcher", "ok", "Лаунчер AutoFPS на місці")
    return _item("launcher", "error", "Немає лаунчера AutoFPS",
                 "Без ~/.local/bin/autofps-launch ігри запускаються без AutoFPS.", "launcher")


def _check_mangohud():
    if shutil.which("mangohud") or os.path.isfile("/usr/bin/mangohud"):
        return _item("mangohud", "ok", "MangoHud встановлено")
    return _item("mangohud", "error", "MangoHud не знайдено",
                 "Без нього AutoFPS не бачить FPS. На SteamOS він є з коробки — "
                 "перевір, чи не видалено пакет або чи оновлено систему.")


def _check_fg():
    mode = _read(os.path.join(C.CFG_DIR, "fg_mode")) or "auto"
    if mode == "off":
        return _item("fg", "ok", "Генерацію кадрів вимкнено в налаштуваннях")
    if mode == "external":
        return _item("fg", "ok", "Генерацією кадрів керує окремий плагін (Decky LSFG-VK / MAKO)")
    ver = _read(os.path.join(C.CFG_DIR, "lsfg_version"))
    if ver in ("1", "2"):
        return _item("fg", "ok", f"lsfg-vk v{ver} знайдено")
    return _item("fg", "warn", "lsfg-vk не встановлено",
                 "Генерація кадрів не працюватиме. Встанови Lossless Scaling у Steam і плагін "
                 "Decky LSFG-VK, у ньому натисни «Install lsfg-vk».")


def _check_game(appid):
    out = []
    opts = _launch_options(appid)
    if opts is not None and "autofps-launch" not in opts:
        out.append(_item("wrapper", "error", "AutoFPS не підключено до цієї гри",
                         "У параметрах запуску немає ~/.local/bin/autofps-launch %command%.",
                         f"wrapper:{appid}"))
    elif opts is not None:
        out.append(_item("wrapper", "ok", "AutoFPS підключено до гри"))

    mode = _read(os.path.join(C.CFG_DIR, f"mode_{appid}"))
    if mode in ("nolsfg", "safe"):
        what = "без генерації кадрів" if mode == "nolsfg" else "у безпечному режимі (AutoFPS нічого не змінює)"
        out.append(_item("mode", "warn", f"Гра запускається {what}",
                         "Режим знизився після двох швидких закриттів гри поспіль. Якщо гра вже "
                         "стабільна — поверни повний режим.", f"reset_mode:{appid}"))
    else:
        out.append(_item("mode", "ok", "Повний режим запуску"))

    tune = C.load_json(C.TUNE_FILE, {})
    entries = [v for k, v in tune.items() if k.startswith(f"{appid}:") and isinstance(v, dict)]
    e = entries[0] if entries else None
    if not e:
        out.append(_item("graphics", "warn", "Гру ще не запускали з AutoFPS",
                         "Запусти гру один раз — AutoFPS виміряє FPS і знайде налаштування графіки."))
    elif e.get("engine") == "unknown":
        out.append(_item("graphics", "warn", "Графіка гри не керується",
                         e.get("note") or "Файл налаштувань не знайдено.", f"retry_graphics:{appid}"))
    else:
        out.append(_item("graphics", "ok", f"Графіка: {e.get('engine')}", e.get("config", "")))
    return out


def _check_log():
    text = _read(WRAPPER_LOG)
    if not text:
        return None
    last = text[text.rfind("=== Запуск гри"):] if "=== Запуск гри" in text else text[-4000:]
    errs = [ln.split("] ", 1)[-1] for ln in last.splitlines()
            if "з помилкою" in ln or "не пройшов перевірку" in ln or "не знайдено" in ln]
    if errs:
        return _item("log", "warn", "Під час останнього запуску були проблеми",
                     "; ".join(dict.fromkeys(errs))[:400])
    return _item("log", "ok", "Останній запуск без помилок")


def check(appid=None):
    items = [_check_launcher(), _check_mangohud(), _check_fg()]
    if appid and str(appid) not in ("0", ""):
        items += _check_game(str(appid))
    lg = _check_log()
    if lg:
        items.append(lg)
    return items


def fix(fix_id, install_launcher=None):
    """Виправлення, що не потребують Steam. Решту (wrapper) робить інтерфейс плагіна."""
    kind, _, appid = str(fix_id).partition(":")
    if kind == "launcher":
        if install_launcher is None:
            return False, "Лаунчер створює сам плагін — перезапусти Decky"
        install_launcher()
        return True, "Лаунчер AutoFPS створено"
    if kind == "reset_mode" and appid.isdigit():
        for fn in (f"mode_{appid}", f"fast_{appid}"):
            try:
                os.remove(os.path.join(C.CFG_DIR, fn))
            except OSError:
                pass
        return True, "Повний режим повернуто — з наступного запуску"
    if kind == "retry_graphics" and appid.isdigit():
        tune = C.load_json(C.TUNE_FILE, {})
        for k, v in tune.items():
            if k.startswith(f"{appid}:") and isinstance(v, dict) and v.get("engine") == "unknown":
                for f in ("engine", "note", "reason", "generic_fmt", "config"):
                    v.pop(f, None)
        C.save_json(C.TUNE_FILE, tune)
        return True, "Пошук налаштувань графіки повториться при наступному запуску"
    return False, f"Невідоме виправлення: {fix_id}"


if __name__ == "__main__":
    print(json.dumps(check(sys.argv[1] if len(sys.argv) > 1 else None), ensure_ascii=False, indent=2))
