"""Спільний код для бекенда (main.py) і скриптів запуску (autotune.py)."""
import json
import os

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


def set_home(home):
    """Бекенд Decky знає домашню теку користувача явно — перевизначаємо шляхи."""
    global HOME, CFG_DIR, CACHE_DIR, LOG_DIR, CURRENT_FILE, SETTINGS_FILE
    global GAMES_FILE, TUNE_FILE, LSFG_CONF, PROFILES_DIR
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
                "quality_target": 40, "start_level": 4}   # 60 базових FPS у важких іграх недосяжні
    return {"id": "generic", "label": f"Невідомий пристрій ({name or '?'})",
            "min_hz": 60, "max_hz": 60, "refresh_control": False,
            "tiers": [30, 40, 60], "res": [1280, 800],
            "quality_target": 40, "start_level": 5}


def quality_target(settings, device):
    """Під скільки справжніх FPS гри підбирати графіку.
    auto — з ліміту від батареї: ліміт на екрані ÷ множник генерації кадрів (не менше 30);
    для VRR/«Авто» — типове значення пристрою."""
    t = str(settings.get("quality_target", "auto"))
    if t != "auto":
        return int(t)
    cap = str(settings.get("bat_mode", "same"))
    if cap == "same":
        cap = str(settings.get("mode", "auto"))
    if cap.isdigit():
        mult = max(1, int(settings.get("last_fg_mult", 1) or 1))
        return max(30, int(cap) // mult)
    return device["quality_target"]
