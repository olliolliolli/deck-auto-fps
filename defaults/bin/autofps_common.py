"""Спільний код для бекенда (main.py) і скриптів запуску (autotune.py)."""
import json
import os
import re

HOME = os.environ.get("AUTOFPS_HOME") or os.path.expanduser("~")
CFG_DIR = os.path.join(HOME, ".config", "autofps")
CACHE_DIR = os.path.join(HOME, ".cache", "autofps")
LOG_DIR = os.path.join(CACHE_DIR, "logs")
CURRENT_FILE = os.path.join(CACHE_DIR, "current.json")
SETTINGS_FILE = os.path.join(CFG_DIR, "settings.json")
GAMES_FILE = os.path.join(CFG_DIR, "games.json")
TUNE_FILE = os.path.join(CFG_DIR, "tune.json")
LSFG_CONF = os.path.join(CFG_DIR, "lsfg-vk.toml")
PROFILES_DIR = os.path.join(CFG_DIR, "profiles")
GAME_SETTINGS_FILE = os.path.join(CFG_DIR, "game_settings.json")

# Налаштування, які можна задати окремо для гри (як «профіль гри» у швидкому меню SteamOS)
PER_GAME_KEYS = ("bat_mode", "ac_mode", "quality_target", "auto_tdp", "auto_gpu_clock",
                 "mango_limiter", "respect_manual_graphics", "lock_config")

# файли налаштувань, які можна робити «лише читання» (гра тоді не перезапише підібране);
# реєстр Wine, сейви й PlayerPrefs — ніколи (там і прогрес, і все інше)
LOCKABLE_EXT = (".ini", ".xml", ".json", ".cfg", ".txt", ".lua", ".config", ".conf")


_FACTS = {}


def game_facts(appid):
    """Факти PCGamingWiki про гру (game_facts.json): e — рушій, dx — версії DirectX, vk — Vulkan,
    up — апскейлери, fg — генерація кадрів, ac — античит, ro — вікі радить «лише для читання»."""
    if "db" not in _FACTS:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "game_facts.json"),
                      encoding="utf-8") as f:
                _FACTS["db"] = json.load(f)
        except (OSError, ValueError):
            _FACTS["db"] = {}
    return _FACTS["db"].get(str(appid), {}) if appid else {}


def lock_files(paths):
    """Зняти дозвіл на запис (гра не перезапише). Повертає список заблокованих."""
    out = []
    for p in paths or []:
        if not p or not p.lower().endswith(LOCKABLE_EXT) or os.path.basename(p).lower() == "user.reg":
            continue
        try:
            st = os.stat(p).st_mode
            os.chmod(p, st & ~0o222)
            out.append(p)
        except OSError:
            pass
    return out


def unlock_files(paths):
    for p in paths or []:
        try:
            st = os.stat(p).st_mode
            os.chmod(p, st | 0o200)
        except OSError:
            pass


def set_home(home):
    """Бекенд Decky знає домашню теку користувача явно — перевизначаємо шляхи."""
    global HOME, CFG_DIR, CACHE_DIR, LOG_DIR, CURRENT_FILE, SETTINGS_FILE
    global GAMES_FILE, TUNE_FILE, LSFG_CONF, PROFILES_DIR, GAME_SETTINGS_FILE
    HOME = home
    CFG_DIR = os.path.join(HOME, ".config", "autofps")
    CACHE_DIR = os.path.join(HOME, ".cache", "autofps")
    LOG_DIR = os.path.join(CACHE_DIR, "logs")
    CURRENT_FILE = os.path.join(CACHE_DIR, "current.json")
    SETTINGS_FILE = os.path.join(CFG_DIR, "settings.json")
    GAMES_FILE = os.path.join(CFG_DIR, "games.json")
    TUNE_FILE = os.path.join(CFG_DIR, "tune.json")
    LSFG_CONF = os.path.join(CFG_DIR, "lsfg-vk.toml")
    PROFILES_DIR = os.path.join(CFG_DIR, "profiles")
    GAME_SETTINGS_FILE = os.path.join(CFG_DIR, "game_settings.json")


def game_profile(appid):
    """Окремі налаштування гри {ключ: значення} або None (гра йде за загальними)."""
    if not appid:
        return None
    d = load_json(GAME_SETTINGS_FILE, {}).get(str(appid))
    if not isinstance(d, dict) or not d.get("enabled"):
        return None
    return {k: v for k, v in d.items() if k in PER_GAME_KEYS}


def settings_for(appid, base=None):
    """Загальні налаштування + окремі налаштування гри поверх них."""
    s = dict(load_json(SETTINGS_FILE, {}) if base is None else base)
    s.update(game_profile(appid) or {})
    return s


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return ""


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


DRM = "/sys/class/drm"


def external_display(drm=None):
    """Підключений зовнішній екран (HDMI / DP через USB-C): {"connector", "w", "h"} або None."""
    import glob as _g
    drm = drm or DRM
    for st in sorted(_g.glob(os.path.join(drm, "card*-*", "status"))):
        name = os.path.basename(os.path.dirname(st))
        if "eDP" in name or "Writeback" in name:
            continue
        try:
            with open(st, encoding="utf-8") as f:
                if f.read().strip() != "connected":
                    continue
            with open(os.path.join(os.path.dirname(st), "modes"), encoding="utf-8") as f:
                first = f.readline().strip()
        except OSError:
            continue
        m = re.match(r"^(\d+)x(\d+)", first)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        return {"connector": name.split("-", 1)[1], "w": w, "h": h}
    return None


TV_RES = [(1280, 720), (1600, 900), (1920, 1080), (2560, 1440), (3840, 2160)]


def tv_render_size(ext, max_h=1080):
    """Роздільність, у якій гра рендерить на телевізорі: не вище max_h (4K-телевізор → 1920×1080,
    gamescope сам масштабує на весь екран)."""
    if not ext or not ext.get("w"):
        return None
    w, h = int(ext["w"]), int(ext["h"])
    max_h = int(max_h or 0)
    if not max_h or h <= max_h:
        return w, h
    return int(round(w * max_h / h / 2)) * 2, max_h


def dock_resolution(handheld, tv_h):
    """Роздільність 16:9 для телевізора замість ручної 16:10 (без чорних смуг):
    найбільша стандартна 16:9 не вища за ручну й не вища за екран телевізора."""
    m = re.match(r"^(\d+)x(\d+)$", str(handheld or ""))
    h = int(m.group(2)) if m else 1080
    cands = [r for r in TV_RES if r[1] <= h and (not tv_h or r[1] <= tv_h)] or [TV_RES[0]]
    w, hh = cands[-1]
    return f"{w}x{hh}"


def panel_vrr_range():
    """Діапазон частот вбудованого екрана з EDID (дескриптор 0xFD): (min, max) Гц або None."""
    import glob as _g
    for path in _g.glob("/sys/class/drm/card*-eDP-*/edid"):
        try:
            with open(path, "rb") as f:
                d = f.read()
        except OSError:
            continue
        if len(d) < 128:
            continue
        for i in range(4):
            b = d[54 + 18 * i: 72 + 18 * i]
            if b[0:3] == b"\0\0\0" and b[3] == 0xFD:
                lo, hi = b[5], b[6]
                if b[4] & 0x01:
                    lo += 255
                if b[4] & 0x02:
                    hi += 255
                if 0 < lo < hi:
                    return lo, hi
    return None


def detect_device():
    name = read_text("/sys/class/dmi/id/product_name")
    family = read_text("/sys/class/dmi/id/product_family")
    version = read_text("/sys/class/dmi/id/product_version")
    ident = f"{name} {family} {version}"

    if name == "Jupiter":
        return {"id": "deck_lcd", "label": "Steam Deck LCD",
                "min_hz": 40, "max_hz": 60, "refresh_control": True,
                "tiers": [30, 40, 60], "res": [1280, 800],
                "quality_target": 40, "start_level": 5}
    if name == "Galileo":
        return {"id": "deck_oled", "label": "Steam Deck OLED",
                "min_hz": 45, "max_hz": 90, "refresh_control": True,
                "tiers": [30, 40, 45, 60, 90], "res": [1280, 800],
                "quality_target": 45, "start_level": 4}
    if "Legion Go S" in ident or name in ("83L3", "83N6", "83Q2", "83Q3"):
        # 120 Гц панель: 30/40/60/120 діляться рівно, тож частоту не міняємо
        return {"id": "legion_go_s", "label": "Lenovo Legion Go S",
                "min_hz": 60, "max_hz": 120, "refresh_control": False, "vrr": True,
                "quiet_profiles": True,   # ручний TDP (custom) гучний — керуємо профілями Lenovo
                # VRR: 47 і 90 теж плавні (екран підлаштовує частоту)
                "tiers": [30, 40, 47, 60, 90, 120], "res": [1920, 1200],
                "vrr_range": panel_vrr_range(),
                # ручна частота GPU: важкі ігри на «авто» тримають 1700–2300 МГц, а вище ~2300
                # SteamOS 3.9.5 обмежує процесор до 1397 МГц (ValveSoftware/SteamOS#2995)
                "gpu_manual_range": (1500, 2300),
                "quality_target": 40, "start_level": 4}   # 60 базових FPS у важких іграх недосяжні
    return {"id": "generic", "label": f"Невідомий пристрій ({name or '?'})",
            "min_hz": 60, "max_hz": 60, "refresh_control": False,
            "tiers": [30, 40, 60], "res": [1280, 800],
            "quality_target": 40, "start_level": 5}


def fg_multiplier_now(settings):
    """Множник генерації кадрів, з яким запускається гра ЗАРАЗ (окремий плагін Decky LSFG-VK
    або MAKO). Обгортка запуску передає AUTOFPS_EXT_FG=1, якщо їхній шар активний."""
    if os.environ.get("AUTOFPS_EXT_FG") != "1":
        return 1
    if settings.get("fg_tool") == "mako":
        return 2
    import re
    text = read_text(os.path.join(HOME, ".config", "lsfg-vk", "conf.toml"))
    cur = re.search(r'^\s*current_profile\s*=\s*"([^"]+)"', text, re.M)
    for b in re.split(r"^\s*\[\[(?:game|profile)\]\]\s*$", text, flags=re.M)[1:]:
        name = re.search(r'^\s*(?:exe|name)\s*=\s*"([^"]+)"', b, re.M)
        mult = re.search(r"^\s*multiplier\s*=\s*(\d+)", b, re.M)
        if mult and (not cur or (name and name.group(1) == cur.group(1))):
            return max(1, int(mult.group(1)))
    return 2


def quality_target_why(settings, device):
    """Пояснення цілі графіки для меню: звідки взялось число."""
    t = str(settings.get("quality_target", "auto"))
    if t != "auto":
        return f"Задано вручну: {t} FPS"
    cap = str(settings.get("bat_mode", "same"))
    if cap == "same":
        cap = str(settings.get("mode", "auto"))
    if cap.isdigit():
        txt = f"Ліміт {cap} FPS — це справжні кадри гри (генерація кадрів не враховується)"
        if int(cap) < 30:
            txt += " → піднято до мінімуму 30"
        return txt
    return f"Ліміт без фіксованого числа (VRR/Авто) — типова ціль пристрою {device['quality_target']}"


def quality_target(settings, device):
    """Під скільки справжніх FPS гри підбирати графіку.
    auto — з ліміту від батареї: ліміт = справжні кадри гри (генерація не враховується, не менше 30);
    для VRR/«Авто» — типове значення пристрою."""
    t = str(settings.get("quality_target", "auto"))
    if t != "auto":
        return int(t)
    cap = str(settings.get("bat_mode", "same"))
    if cap == "same":
        cap = str(settings.get("mode", "auto"))
    if cap.isdigit():
        return max(30, int(cap))          # ліміт — справжні кадри гри, генерацію не враховуємо
    return device["quality_target"]
