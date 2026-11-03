"""
AutoFPS — Decky Loader plugin
Автоматично підбирає стабільний рівень FPS (30/40/45/60/90/120) для гри
на Steam Deck (LCD/OLED) і Lenovo Legion Go S, виставляє ліміт кадрів
і частоту екрана через gamescope, вмикає фрейм-генерацію (lsfg-vk)
там, де нативно гра не тягне верхній рівень.

Як воно міряє FPS: гра запускається через обгортку bin/autofps,
яка вмикає MangoHud-лог (невидимий) у ~/.cache/autofps/logs.
"""

import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

import decky

_PLUGIN_DIR = getattr(decky, "DECKY_PLUGIN_DIR", os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_PLUGIN_DIR, "bin"), os.path.join(_PLUGIN_DIR, "defaults", "bin")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
import autofps_common as C  # noqa: E402
import optiscaler as O  # noqa: E402
import power as P  # noqa: E402

C.set_home(decky.DECKY_USER_HOME)
HOME = C.HOME
CFG_DIR, CACHE_DIR, LOG_DIR = C.CFG_DIR, C.CACHE_DIR, C.LOG_DIR
CURRENT_FILE, SETTINGS_FILE, GAMES_FILE = C.CURRENT_FILE, C.SETTINGS_FILE, C.GAMES_FILE
TUNE_FILE, LSFG_CONF, PROFILES_DIR = C.TUNE_FILE, C.LSFG_CONF, C.PROFILES_DIR
APPS_FILE = os.path.join(CFG_DIR, "apps.json")
# Обмежувач FPS усередині MangoHud (обгортка підключає цей файл; MangoHud стежить за ним через
# inotify і підхоплює зміну за ~0,1 с). Потрібен, коли ліміт gamescope не діє — напр. із шаром
# Decky LSFG-VK меню й легкі сцени йшли 120 FPS при CPU 21 Вт. MangoHud бачить лише справжні
# кадри гри, тож ліміт = базовий FPS, а генерація кадрів множить уже після нього.
MANGO_LIMIT_FILE = os.path.join(CACHE_DIR, "mangohud_limit.conf")
_mango_last = [None]


def write_mango_limit(fps):
    """fps = 0 — без обмеження. Пише лише за зміни і лише у файл, створений обгорткою
    (інакше MangoHud за ним не стежить)."""
    fps = int(fps or 0)
    if _mango_last[0] == fps or not os.path.isfile(MANGO_LIMIT_FILE):
        return False
    try:
        with open(MANGO_LIMIT_FILE, "w", encoding="utf-8") as f:   # на місці: inotify IN_MODIFY
            f.write(f"fps_limit={fps}\n")
        _mango_last[0] = fps
        return True
    except OSError as e:
        decky.logger.warning(f"MangoHud limit: {e}")
        return False
LAUNCHER = os.path.join(HOME, ".local", "bin", "autofps-launch")
LAUNCH_OPTION = "~/.local/bin/autofps-launch %command%"


def install_launcher():
    """Стабільний лаунчер у домашній теці (не залежить від назви теки плагіна і прав).
    Якщо плагін видалять — гра все одно запуститься як звичайно."""
    real = os.path.join(_PLUGIN_DIR, "bin", "autofps")
    text = (
        "#!/bin/bash\n"
        "# AutoFPS launcher (створено плагіном AutoFPS)\n"
        f'REAL={json.dumps(real)}\n'
        'if [ -f "$REAL" ]; then exec bash "$REAL" "$@"; fi\n'
        'exec "$@"\n'
    )
    os.makedirs(os.path.dirname(LAUNCHER), exist_ok=True)
    if _read(LAUNCHER) != text.strip():
        with open(LAUNCHER, "w", encoding="utf-8") as f:
            f.write(text)
    os.chmod(LAUNCHER, 0o755)  # {appid: {"enabled": bool, "seen": ts}}

_read, _load_json, _save_json = C.read_text, C.load_json, C.save_json
percentile, detect_device = C.percentile, C.detect_device

DEFAULT_SETTINGS = {
    "enabled": True,
    "mode": "auto",            # "auto" | "vrr" (VRR, без обмеження до частоти екрана) | фіксований рівень, напр. "60"
    "auto_tdp": "always",
    "auto_gpu_clock": True,    # найнижча фіксована частота GPU, з якою FPS і фреймтайм рівні      # найтихіше живлення, що тримає FPS: always | battery (лише від батареї) | off
    "power_profiles": True,    # окремі рівні FPS від батареї й від зарядки
    "bat_mode": "same",        # FPS від батареї: same (як «mode») | auto | 30 | 40 | …
    "ac_mode": "same",         # FPS від зарядки
    "community_profiles": True,  # готові профілі ігор з бази AutoFPS на GitHub
    "headroom": 0.95,          # p10 FPS має бути >= рівень * headroom
    "drop_ratio": 0.88,        # якщо p10 < рівень * drop_ratio — знижуємо
    "warmup_s": 20,            # пропустити завантаження/шейдери
    "probe_s": 20,             # скільки секунд міряти без ліміту
    "start_on_button": True,   # підбір FPS починається лише після кнопки «Почати підбір» (не в меню гри)
    "fg_tool": "lsfg",         # чий лаунчер FG додавати в параметри запуску: lsfg | mako | none
    "fg_mode": "external",     # "external" (окремий плагін Decky LSFG-VK) | "off" | "auto" | "always"
    "fg_min_base": 40,         # мінімальний базовий FPS для фрейм-генерації
    "fg_allow_3x": False,
    "fg_flow_scale": 0.8,
    "fg_performance_mode": True,
    "mango_limiter": True,          # обмежувач FPS у MangoHud, коли ліміт gamescope не діє
    "fps_counts_generated": False,  # MangoHud бачить лише справжні кадри гри (перевірено на Legion: 42 у MangoHud = 84 на екрані з Lossless ×2)
    "autotune": True,          # автопідбір графіки гри (Unreal/Unity)
    "ue5_tweak": True,         # твік Engine.ini для ігор на Unreal Engine 5
    "unity_tweak": True,       # твік boot.config для ігор на Unity
    "quality_target": "auto",  # базовий FPS, під який підбирається графіка
    "respect_manual_graphics": False,
    "lowres_for_quality": True,  # екран 1080p+: пробувати рендер ≈HD, якщо так графіка вища  # True — ручні зміни в меню гри зупиняють підбір
    "opti_upscaler": "auto",   # auto (FSR 4.1) | fsr4 | fsr40 | fsr31 | xess — чим замінювати DLSS
    "opti_method": "auto",     # auto (гра з FSR 3.1 → «один файл» FSR 4) | direct | optiscaler
    "opti_gpu_arch": "auto",   # auto | rdna2 | rdna3 | rdna4 — якщо автовизначення помилилось
    "opti_fg": False,
    "ref_sharpness": "sharp",
    "bridge_version": "4",     # міст FSR2→FSR4 (RE4): "4" — FSR 4 (краща картинка) | "3" — FSR 3.1 (+~15% FPS)  # ігри Capcom з REFramework: sharp (рендер 77%) | native (100%) | auto          # генерація кадрів OptiScaler (тоді lsfg-vk у цій грі вимикається)
}


def best_refresh(device, out_fps):
    """Найвища частота екрана, кратна out_fps, у межах панелі."""
    if not device["refresh_control"] or out_fps <= 0:
        return None
    k = device["max_hz"] // out_fps
    while k >= 1:
        hz = out_fps * k
        if device["min_hz"] <= hz <= device["max_hz"]:
            return hz
        k -= 1
    return device["max_hz"]


# ------------------------------------------------------- gamescope control

def _clean_env():
    env = dict(os.environ)
    # Decky підмішує свої бібліотеки — системним бінарникам вони заважають
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    env["DISPLAY"] = ":0"
    env.setdefault("XAUTHORITY", os.path.join(HOME, ".Xauthority"))
    return env


def set_root_atom(name, value):
    xprop = shutil.which("xprop") or "/usr/bin/xprop"
    cmd = [xprop, "-root", "-f", name, "32c", "-set", name, str(int(value))]
    try:
        r = subprocess.run(cmd, env=_clean_env(), capture_output=True,
                           text=True, timeout=3)
        if r.returncode != 0:
            decky.logger.warning(f"xprop {name}={value}: {r.stderr.strip()}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        decky.logger.warning(f"xprop failed: {e}")
        return False


def apply_display(device, fps_limit, refresh, vrr=None):
    if vrr is not None and device.get("vrr"):
        set_root_atom("GAMESCOPE_VRR_ENABLED", 1 if vrr else 0)
    set_root_atom("GAMESCOPE_FPS_LIMIT", fps_limit)
    if refresh:
        set_root_atom("GAMESCOPE_DYNAMIC_REFRESH", refresh)


# ------------------------------------------------------------ lsfg-vk
#
# Decky LSFG-VK у різних версіях ставить різний lsfg-vk:
#   до 0.12  — lsfg-vk v1: VkLayer_LS_frame_generation.json, Lossless.dll,
#              конфіг version = 1 з [[game]] exe = …, вибір через LSFG_PROCESS;
#   0.13.x   — ранній v2: VkLayer_LSFGVK_…, Lossless.dll, version = 2, [[profile]];
#   0.14+    — v2.0.0: те саме, але lsfg-vk.dll з бета-гілки Lossless Scaling.
# У v2 множник має бути >= 2 (1 — невалідний конфіг), тому генерація кадрів
# вмикається/вимикається при запуску гри, а під час гри змінюється лише множник.

LSFG_PROFILE = "autofps"
LSFG_VERSION_FILE = os.path.join(CFG_DIR, "lsfg_version")   # читає обгортка гри
FG_MODE_FILE = os.path.join(CFG_DIR, "fg_mode")             # off | auto | always
THEIR_CONF = os.path.join(HOME, ".config", "lsfg-vk", "conf.toml")
# лаунчер Decky LSFG-VK у параметрах запуску: 0.14+ — ~/.lsfg, старіші — ~/lsfg
LSFG_LAUNCHERS = [("~/.lsfg", os.path.join(HOME, ".lsfg")), ("~/lsfg", os.path.join(HOME, "lsfg")),
                  ("~/.local/bin/lsfg", os.path.join(HOME, ".local", "bin", "lsfg"))]


MAKO_LAUNCHERS = [("~/.local/bin/mako-run", os.path.join(HOME, ".local", "bin", "mako-run"))]


def fg_launchers():
    """Знайдені лаунчери окремих плагінів генерації кадрів: {"lsfg": "~/.lsfg", "mako": "…"}."""
    out = {}
    for tool, lst in (("lsfg", LSFG_LAUNCHERS), ("mako", MAKO_LAUNCHERS)):
        out[tool] = next((tok for tok, path in lst if os.path.isfile(path)), "")
    return out


def lsfg_launcher(tool="lsfg"):
    """Лаунчер, який AutoFPS додає в параметри запуску разом зі своїм (за вибором fg_tool)."""
    if tool == "none":
        return ""
    return fg_launchers().get(tool, "")


def their_multiplier():
    """Множник генерації кадрів у поточному профілі Decky LSFG-VK (типово 2)."""
    if _load_json(SETTINGS_FILE, {}).get("fg_tool") == "mako":
        return 2   # MAKO: множник у його плагіні, типово ×2
    text = _read(THEIR_CONF)
    cur = re.search(r'^\s*current_profile\s*=\s*"([^"]+)"', text, re.M)
    blocks = re.split(r"^\s*\[\[(?:game|profile)\]\]\s*$", text, flags=re.M)[1:]
    for b in blocks:
        name = re.search(r'^\s*(?:exe|name)\s*=\s*"([^"]+)"', b, re.M)
        mult = re.search(r"^\s*multiplier\s*=\s*(\d+)", b, re.M)
        if mult and (not cur or (name and name.group(1) == cur.group(1))):
            return max(1, int(mult.group(1)))
    return 2
LSFG_CLI = os.path.join(HOME, ".local", "bin", "lsfg-vk-cli")


def _lsfg_dll_path():
    """DLL з конфіга Decky LSFG-VK — саме з ним у користувача все працює."""
    m = re.search(r'^\s*dll\s*=\s*"([^"]+)"', _read(THEIR_CONF), re.M)
    return m.group(1) if m and os.path.isfile(m.group(1)) else ""


def _their_conf_version():
    m = re.search(r"^\s*version\s*=\s*(\d+)", _read(THEIR_CONF), re.M)
    return int(m.group(1)) if m else None


def _layer_dirs():
    dirs = [os.path.join(HOME, ".local/share/vulkan/implicit_layer.d"),
            "/usr/share/vulkan/implicit_layer.d", "/usr/local/share/vulkan/implicit_layer.d",
            "/etc/vulkan/implicit_layer.d"]
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        dirs.insert(0, os.path.join(xdg, "vulkan/implicit_layer.d"))
    return dirs


def _find_layers():
    """Імена файлів Vulkan-шарів (lsfg-vk / MAKO), знайдених у системі."""
    found = []
    for d in _layer_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(".json"):
                continue
            low = fn.lower()
            if "lsfg" in low or "mako" in low or low.startswith("vklayer_ls_"):
                found.append(fn)
                continue
            text = _read(os.path.join(d, fn)).lower()
            if '"name"' in text and ("lsfg" in text or "lossless" in text):
                found.append(fn)
    return found


def lsfg_version(layers=None):
    """1, 2 або None — яка версія lsfg-vk встановлена."""
    layers = _find_layers() if layers is None else layers
    low = [fn.lower() for fn in layers if "mako" not in fn.lower()]
    has_v2 = any("lsfgvk" in fn for fn in low) or os.path.isfile(LSFG_CLI)
    has_v1 = any(fn.startswith("vklayer_ls_") for fn in low) or \
        os.path.isfile(os.path.join(HOME, ".local/lib/liblsfg-vk.so"))
    if has_v2 and has_v1:
        return 1 if _their_conf_version() == 1 else 2
    if has_v2:
        return 2
    if has_v1 or low:
        return 1
    return None


def _steam_libraries():
    steam = os.path.join(HOME, ".local", "share", "Steam")
    libs = [os.path.join(steam, "steamapps")]
    vdf = _read(os.path.join(steam, "steamapps", "libraryfolders.vdf"))
    for path in re.findall(r'"path"\s+"([^"]+)"', vdf):
        lib = os.path.join(path, "steamapps")
        if lib not in libs:
            libs.append(lib)
    return libs


def _decky_plugins():
    """Назви встановлених Decky-плагінів (з plugin.json)."""
    names = []
    base = os.path.join(HOME, "homebrew", "plugins")
    try:
        dirs = os.listdir(base)
    except OSError:
        return names
    for d in dirs:
        meta = _load_json(os.path.join(base, d, "plugin.json"), None)
        if meta:  # тека без plugin.json — залишок, не плагін
            names.append(str(meta.get("name") or d))
    return names


def _dll_candidates(version):
    """DLL у порядку пріоритету: з конфіга Decky LSFG-VK, далі з бібліотек Steam."""
    out = []
    their = _lsfg_dll_path()
    if their:
        out.append(their)
    names = ["lsfg-vk.dll", "Lossless.dll"] if version == 2 else ["Lossless.dll"]
    for lib in _steam_libraries():
        for n in names:
            cand = os.path.join(lib, "common", "Lossless Scaling", n)
            if os.path.isfile(cand) and cand not in out:
                out.append(cand)
    return out


def _render_lsfg(version, dll, multiplier, settings):
    mult = max(2, int(multiplier))  # 1 у v2 — помилка конфіга
    flow = max(0.25, min(1.0, float(settings["fg_flow_scale"])))
    perf = "true" if settings["fg_performance_mode"] else "false"
    if version == 1:
        lines = ["version = 1", "", "[global]"]
        if dll:
            lines.append(f"dll = {json.dumps(dll)}")
        lines += ["", "[[game]]", f'exe = "{LSFG_PROFILE}"', f"multiplier = {mult}",
                  f"flow_scale = {flow:.2f}", f"performance_mode = {perf}"]
    else:
        lines = ["version = 2", "", "[global]"]
        if dll:
            lines.append(f"dll = {json.dumps(dll)}")
        lines += ["", "[[profile]]", f'name = "{LSFG_PROFILE}"', f"multiplier = {mult}",
                  f"flow_scale = {flow:.2f}", f"performance_mode = {perf}"]
    return "\n".join(lines) + "\n"


def _cli_validate(text):
    """(ok, повідомлення) від lsfg-vk-cli validate — так само перевіряє Decky LSFG-VK."""
    if not os.access(LSFG_CLI, os.X_OK):
        return None, "lsfg-vk-cli не знайдено"
    tmp = LSFG_CONF + ".check"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        r = subprocess.run([LSFG_CLI, "validate", "--config", tmp, "--print"],
                           env=_clean_env(), capture_output=True, text=True, timeout=10)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            return False, out or f"код {r.returncode}"
        if "The DLL file does not exist" in out or "DLL override: (none)" in out:
            return False, out
        return True, out
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


_LSFG_STATE = {"t": 0.0, "state": None}


def lsfg_setup(settings, force=False):
    """Визначає версію lsfg-vk і робочий DLL (кеш 60 с). Результат:
    {"version", "dll", "valid", "detail"}. valid=None — перевірити нема чим (v1)."""
    now = time.time()
    if not force and _LSFG_STATE["state"] and now - _LSFG_STATE["t"] < 60:
        return _LSFG_STATE["state"]
    version = lsfg_version()
    state = {"version": version, "dll": "", "valid": None, "detail": ""}
    if version == 2 and os.access(LSFG_CLI, os.X_OK):
        tried = []
        for dll in _dll_candidates(2) + [""]:
            ok, msg = _cli_validate(_render_lsfg(2, dll, 2, settings))
            tried.append(f"{dll or '(автопошук)'}: {'OK' if ok else msg[:160]}")
            if ok:
                state.update(dll=dll, valid=True)
                break
        else:
            state["valid"] = False
        state["detail"] = " | ".join(tried)
    elif version:
        cands = _dll_candidates(version)
        state["dll"] = cands[0] if cands else ""
        state["valid"] = bool(cands) if version == 1 else None
    _LSFG_STATE.update(t=now, state=state)
    return state


def write_lsfg(settings, multiplier):
    """Наш конфіг lsfg-vk у форматі встановленої версії. Множник >= 2.
    Обгортка гри вмикає його лише коли генерація кадрів потрібна в цій сесії."""
    st = lsfg_setup(settings)
    version = st["version"] or 2
    text = _render_lsfg(version, st["dll"], multiplier, settings)
    os.makedirs(CFG_DIR, exist_ok=True)
    if _read(LSFG_CONF) != text.strip():
        with open(LSFG_CONF, "w", encoding="utf-8") as f:
            f.write(text)
    ver = str(st["version"] or "")
    if _read(LSFG_VERSION_FILE) != ver:
        with open(LSFG_VERSION_FILE, "w", encoding="utf-8") as f:
            f.write(ver)


def lsfg_status(settings=None):
    """Детальна перевірка фрейм-генерації: що знайдено і що зробити."""
    settings = settings or DEFAULT_SETTINGS
    layers = _find_layers()
    has_mako = any("mako" in fn.lower() for fn in layers) or \
        os.path.exists(os.path.join(HOME, ".local/bin/mako-launch"))
    plugins = _decky_plugins()
    lsfg_plugin = next((p for p in plugins if "lsfg" in p.lower()), None)
    mako_plugin = next((p for p in plugins if "mako" in p.lower()), None)
    st = lsfg_setup(settings)
    ver = st["version"]

    if ver == 2 and st["valid"]:
        hint, ready = f"lsfg-vk v2 готовий (DLL: {os.path.basename(st['dll']) or 'автопошук'})", True
    elif ver == 2 and st["valid"] is None:
        hint, ready = "lsfg-vk v2 знайдено, але без lsfg-vk-cli перевірити конфіг нема чим", False
    elif ver == 2:
        hint, ready = ("lsfg-vk v2 є, але жоден DLL не підійшов. Відкрий Decky LSFG-VK і перевір, "
                       "що там усе працює (за потреби — бета-гілка «lsfg-vk» у Lossless Scaling). "
                       "Деталі — у діагностиці"), False
    elif ver == 1 and st["dll"]:
        hint, ready = f"lsfg-vk v1 готовий (DLL: {os.path.basename(st['dll'])})", True
    elif ver == 1:
        hint, ready = ("lsfg-vk v1 є, але не знайдено Lossless.dll. Встанови Lossless Scaling у Steam "
                       "(потрібна купівля)"), False
    elif lsfg_plugin:
        hint, ready = (f"Плагін «{lsfg_plugin}» є, але сам lsfg-vk не встановлено. "
                       "Відкрий його в Decky і натисни «Install lsfg-vk»"), False
    elif has_mako or mako_plugin:
        hint, ready = ("Знайдено MAKO, а не lsfg-vk. AutoFPS керує генерацією кадрів лише через "
                       "lsfg-vk: встанови «Decky LSFG-VK» з магазину Decky. MAKO не вмикай для тих "
                       "самих ігор — вони конфліктують"), False
    else:
        hint, ready = ("lsfg-vk не знайдено. Встанови «Decky LSFG-VK» з магазину Decky і натисни "
                       "в ньому «Install lsfg-vk» (потрібен Lossless Scaling у Steam)"), False
    return {"ready": ready, "hint": hint, "version": ver, "layers": layers,
            "plugins": [p for p in (lsfg_plugin, mako_plugin) if p], "dll": st["dll"],
            "detail": st["detail"]}


def lsfg_installed():
    return lsfg_status()["ready"]


# ------------------------------------------------------- MangoHud reader

class LogReader:
    def __init__(self):
        self.path = None
        self.offset = 0
        self.cols = None
        self.buf = ""
        self.hw = []      # [(частота GPU МГц, частота CPU МГц, навантаження GPU %)] — забирає _tick

    def reset(self):
        self.__init__()

    def _newest(self, since):
        try:
            files = [os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR)
                     if f.endswith(".csv")]
        except OSError:
            return None
        files = [f for f in files if os.path.getmtime(f) >= since - 5]
        return max(files, key=os.path.getmtime) if files else None

    def read(self, since):
        """Повертає нові рядки як [(fps, gpu_load|None), ...]."""
        newest = self._newest(since)
        if not newest:
            return []
        if newest != self.path:
            self.path, self.offset, self.cols, self.buf = newest, 0, None, ""
        try:
            with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self.offset)
                chunk = f.read()
                self.offset = f.tell()
        except OSError:
            return []
        self.buf += chunk
        lines = self.buf.split("\n")
        self.buf = lines.pop()  # неповний рядок лишаємо на потім
        out = []
        for line in lines:
            parts = line.strip().split(",")
            if not parts or not parts[0]:
                continue
            if parts[0] == "fps":
                self.cols = {c: i for i, c in enumerate(parts)}
                continue
            if self.cols is None:
                continue
            try:
                fps = None
                if "frametime" in self.cols:
                    ft = float(parts[self.cols["frametime"]])
                    if ft > 0:
                        fps = 1000.0 / ft
                if fps is None:
                    fps = float(parts[self.cols["fps"]])
                gpu = None
                if "gpu_load" in self.cols:
                    gpu = float(parts[self.cols["gpu_load"]])
                if 0 < fps < 1000:
                    out.append((fps, gpu))
                    clk = float(parts[self.cols["gpu_core_clock"]]) if "gpu_core_clock" in self.cols else None
                    cpu = float(parts[self.cols["cpu_mhz"]]) if "cpu_mhz" in self.cols else None
                    self.hw.append((clk, cpu, gpu))
            except (ValueError, IndexError, KeyError):
                continue
        return out


# ---------------------------------------------------------------- plugin

class Plugin:
    async def _main(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        os.makedirs(PROFILES_DIR, exist_ok=True)
        self.device = detect_device()
        self.settings = {**DEFAULT_SETTINGS, **_load_json(SETTINGS_FILE, {})}
        # генерацією кадрів тепер керує окремий плагін Decky LSFG-VK (одноразовий перехід)
        if not self.settings.get("fg_external_migrated"):
            self.settings.update(fg_mode="external", fg_external_migrated=True)
            _save_json(SETTINGS_FILE, self.settings)
        # екран з VRR (Legion Go S): за замовчуванням — VRR без обмеження до 120 FPS (одноразово)
        if not self.settings.get("fps_fix_migrated"):
            # раніше FPS з MangoHud ділився на множник Lossless — заміри графіки були вдвічі заниженими
            self.settings.update(fps_counts_generated=False, fps_fix_migrated=True)
            _save_json(SETTINGS_FILE, self.settings)
            tune = _load_json(TUNE_FILE, {})
            for e in tune.values():
                if isinstance(e, dict) and not e.get("from_community"):
                    for k in ("last", "history", "failed", "ok", "converged", "ineffective",
                              "native_result", "lowres_done", "track"):
                        e.pop(k, None)
                    e["note"] = "Заміри FPS виправлено — графіку буде підібрано заново"
            _save_json(TUNE_FILE, tune)
            games = _load_json(GAMES_FILE, {})
            for g in games.values():
                if isinstance(g, dict):
                    g.pop("tdp", None)
            _save_json(GAMES_FILE, games)
        if not self.settings.get("ui_fps_migrated"):
            # новий інтерфейс: окремо ліміт від батареї й від зарядки; графіка — під ліміт від батареї
            for k in ("bat_mode", "ac_mode"):
                if self.settings.get(k, "same") == "same":
                    self.settings[k] = self.settings.get("mode", "auto")
            self.settings.update(power_profiles=True, quality_target="auto", ui_fps_migrated=True)
            _save_json(SETTINGS_FILE, self.settings)
        if not self.settings.get("power_migrated"):   # тиша й батарея — завжди, не лише від батареї
            self.settings.update(auto_tdp="always", power_migrated=True)
            _save_json(SETTINGS_FILE, self.settings)
        if self.device.get("vrr") and not self.settings.get("vrr_migrated"):
            self.settings.update(mode="vrr", bat_mode="vrr", ac_mode="vrr", vrr_migrated=True)
            _save_json(SETTINGS_FILE, self.settings)
        if self.settings.pop("fps_calib", None) is not None:   # автоперевірку MangoHud прибрано
            _save_json(SETTINGS_FILE, self.settings)
        self.games = _load_json(GAMES_FILE, {})
        self.reader = LogReader()
        self.ac = P.on_ac()
        self.ac_checked = 0.0
        self.pw = P.PowerCtl(self.device, decky.logger.info)
        self.gc = P.GpuClock(self.pw.tdp, decky.logger.info)
        self.gpu_orig = None
        decky.logger.info(f"частота GPU: {'%s–%s МГц' % (self.gc.min, self.gc.max) if self.gc.available else self.gc.error}")
        self.started_t = time.time()
        self.tdp_orig = None
        self._restore_after_restart()
        self.telemetry = {}
        decky.logger.info(f"живлення: {self.pw.kind or 'немає керування'} "
                          f"[{', '.join(self.pw.label(i) for i in range(len(self.pw.levels)))}] {self.pw.error}")
        self._reset_game_state()
        self.last_appid = None
        self.events = []
        self.loop = asyncio.get_event_loop()
        self.last_crash_t = int(time.time())
        try:
            install_launcher()
        except Exception as e:  # noqa: BLE001
            decky.logger.error(f"launcher install failed: {e}")
        write_lsfg(self.settings, 2)
        self._write_fg_mode()
        decky.logger.info(f"AutoFPS started on {self.device['label']}")
        self.task = asyncio.get_event_loop().create_task(self._loop())

    async def _unload(self):
        if getattr(self, "task", None):
            self.task.cancel()
        self._restore_display()
        self._tdp_restore()
        self._gpu_restore()

    async def _uninstall(self):
        self._restore_display()

    # ---------------- state

    def _reset_game_state(self):
        self.appid = None
        self.pid = None
        self.game_started = 0.0
        self.phase = "idle"       # idle|warmup|probe|locked|manual
        self.phase_t = 0.0
        self.samples = []         # [(t, fps, gpu)]
        self.base = 0             # базовий (реальний) FPS
        self.mult = 1             # множник фрейм-генерації
        self.native_p10 = 0.0
        self.bad_windows = 0
        self.idle_windows = 0
        self.last_probe = 0.0
        self.last_apply = 0.0
        self.tuning = False       # цей запуск — крок автопідбору графіки
        self.manual_measured = False  # фіксований FPS: замір для автопідбору вже зроблено
        self.user_started = False     # натиснуто «Почати підбір» у цій сесії гри
        self.warned_no_data = False
        self.cur_info = {}
        self.sfg = 0              # множник генерації кадрів, увімкненої обгорткою в цій сесії
        self.message = "Гра не запущена через AutoFPS"
        self.manual_key = None
        self.tdp_state = "off"    # off | max | descending | settled
        self.tdp_cur = None
        self.tdp_last = 0.0
        self.tdp_floor = 0        # нижче цього вже не тримало
        self.tdp_samples = []
        self.tdp_key = None
        self.tdp_ref = None       # VRR: p10 на повному живленні
        self.hot, self.hot_since, self.hot_warned = False, 0, False
        self.calib = None
        self.tm_raw, self.tm_windows, self.tm_t, self.tm_saved_n = [], [], 0.0, 0
        self.tm_forecast = None
        self.tm_pending = None
        self.tm_over = []
        self.mango_cap, self.cap_over, self.cap_t = False, 0, 0.0
        _mango_last[0] = None     # обгортка щойно записала у файл fps_limit=0
        self.menu_t, self.menu_mtime, self.menu_changed = 0.0, None, False
        self.gpu_state = "off"    # off | ref | descending | settled
        self.gpu_cur = None       # МГц (None — авто)
        self.gpu_ok = None        # остання частота, що тримала
        self.gpu_ref = None       # (p10, p5) на авто-частоті
        self.gpu_samples, self.gpu_t, self.gpu_key, self.gpu_bad = [], 0.0, None, 0
        self.gpu_hw = []          # [(t, частота GPU, частота CPU, навантаження GPU)]
        self.gpu_ref_clk = None   # частота, яку GPU сам тримав під навантаженням на «авто»
        self.perf_typical, self.tdp_bad = None, 0
        self.gpu_ref_w = []

    def _restore_display(self):
        write_mango_limit(0)
        try:
            apply_display(self.device, 0, self.device["max_hz"]
                          if self.device["refresh_control"] else None)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- сповіщення

    def _notify(self, title, body):
        """Тост у Steam + запис у журнал плагіна."""
        self.events.append({"t": int(time.time()), "title": title, "body": body})
        decky.logger.info(f"[{self.appid or '-'}] {title}: {body.replace(chr(10), ' / ')}")
        del self.events[:-12]
        emit = getattr(decky, "emit", None)
        loop = getattr(self, "loop", None)
        if emit and loop:
            try:
                loop.create_task(emit("autofps_toast", title, body))
            except Exception as e:  # noqa: BLE001
                decky.logger.warning(f"emit failed: {e}")

    def _opti_selfcheck(self, now):
        """Після виходу з гри: перевірити журнал OptiScaler і, якщо треба, змінити назву DLL."""
        if not (self.cur_info.get("opti") or self.cur_info.get("opti_method") == "direct"):
            return
        crash = _load_json(os.path.join(CACHE_DIR, "last_crash.json"), None) or {}
        crashed = (str(crash.get("appid")) == str(self.appid)
                   and int(crash.get("t", 0)) >= int(self.game_started))
        try:
            res = O.analyze(self.appid, self.game_started, now - self.game_started, crashed,
                            decky.logger.info)
        except Exception as e:  # noqa: BLE001
            decky.logger.error(f"OptiScaler self-check failed: {e}")
            return
        if res:
            self._notify(f"AutoFPS: {res['title']}", res["message"])

    def _report_crash(self):
        crash = _load_json(os.path.join(CACHE_DIR, "last_crash.json"), None)
        if not crash or int(crash.get("t", 0)) <= self.last_crash_t:
            return
        self.last_crash_t = int(crash["t"])
        nxt = {"nolsfg": "без генерації кадрів",
               "safe": "без втручання AutoFPS (безпечний режим)"}.get(crash.get("to"), "")
        self._notify("AutoFPS: гра закрилась одразу",
                     f"Гра працювала {crash.get('dur')} с (код {crash.get('rc')}). "
                     + (f"Наступний запуск — {nxt}. " if nxt else "")
                     + "Збери діагностику й надішли Claude")

    def _mode_report(self, cur):
        mode = cur.get("mode", "full")
        parts = []
        if mode == "nolsfg":
            parts.append("Режим без генерації кадрів (минулого разу гра впала на старті)")
        elif mode == "safe":
            parts.append("Безпечний режим: AutoFPS нічого не змінює (гра падала на старті)")
        elif int(cur.get("fg_want", 0) or 0) >= 2 and not cur.get("lsfg"):
            parts.append("Генерацію кадрів не ввімкнено: lsfg-vk не пройшов перевірку — деталі в меню")
        elif int(cur.get("fg", 0) or 0) >= 2:
            parts.append(f"Генерація кадрів ×{cur.get('fg')} увімкнена")
        if cur.get("opti_method") == "bridge":
            parts.append("FSR 4 через міст (інтерфейс чіткий): у налаштуваннях гри має бути FSR 2")
        elif cur.get("opti_method") == "direct":
            g = O.load_game(self.appid)
            parts.append("FSR 4 → " + (g.get("upscaler_label") or "один файл") +
                         ": у налаштуваннях гри вибери FSR"
                         + ("" if g.get("direct_confirmed") else " — у кутку має з'явитися напис AMD «FSR 4»"))
        elif cur.get("opti"):
            parts.append("DLSS → " + (O.load_game(self.appid).get("upscaler_label") or "FSR") +
                         " (OptiScaler): у налаштуваннях гри вибери DLSS")
        if cur.get("mangohud") == 0 and mode != "safe":
            parts.append("MangoHud недоступний — FPS не вимірюється")
        return parts

    def _graphics_report(self):
        """Що зроблено з графікою гри на цьому запуску (пише bin/autotune.py)."""
        if not self.settings["autotune"]:
            return "Автопідбір графіки вимкнено"
        e = self._tune_entry()
        if not e:
            return "Графіку не змінено"
        eng = {"unreal": "Unreal", "unity": "Unity", "re_engine": "RE Engine",
               "generic:fromsoft": "FromSoftware", "generic:xmltext": "XML", "generic:xmlattr": "XML",
               "generic:kvline": "конфіг", "generic:ini": "INI", "generic:json": "JSON",
               "generic:func": "video.scr", "generic:winreg": "реєстр"}.get(e.get("engine"), "")
        if e.get("engine") == "unknown":
            return "Рушій гри не підтримується — графіку не змінюю, лише FPS"
        if e.get("user_override"):
            return "Графіка: твої налаштування з меню гри — не чіпаю"
        if e.get("disabled"):
            return "Графіка: оригінальні налаштування гри"
        if e.get("applied_level") is None:
            return e.get("note") or "Графіку не змінено"
        step = f"сходинка {e['applied_level'] + 1}/{e.get('steps', '?')}"
        if e.get("lowres"):
            step += ", рендер ≈HD заради графіки"
        verb = "Змінено графіку" if e.get("changed") else "Графіка без змін"
        return f"{verb} ({eng}, {step}): {e.get('summary', '')}. {e.get('note', '')}".strip()

    def _out_fps(self):
        return self.base * self.mult

    def _mango_cap_value(self):
        """Ліміт для обмежувача MangoHud (справжні кадри гри) або 0."""
        if not getattr(self, "mango_cap", False) or self.phase == "probe" or not self.base:
            return 0
        if self._vrr():
            return int(self.device["max_hz"] // max(1, self.mult))
        return int(self.base)

    def _apply(self):
        out = self._out_fps()
        write_mango_limit(self._mango_cap_value())
        if self._vrr():
            # VRR: частота екрана йде за грою, обмежуємо лише зверху — частотою панелі
            apply_display(self.device, self.device["max_hz"], None, vrr=True)
            self.last_apply = time.time()
            return
        if self.device.get("vrr"):
            # фіксований ліміт теж з VRR: 47/90 FPS на 120 Гц без нерівних кадрів
            limit = 0 if self.phase == "probe" else out
            apply_display(self.device, limit, None, vrr=True)
            self.last_apply = time.time()
            return
        limit = 0 if self.phase == "probe" else out
        refresh = best_refresh(self.device,
                               self.device["max_hz"] if self.phase == "probe" else out)
        apply_display(self.device, limit, refresh)
        if self.sfg >= 2 and self.settings["fg_mode"] != "external":
            write_lsfg(self.settings, max(2, self.mult))   # множник змінюється на льоту (лише >= 2)
        self.last_apply = time.time()

    def _write_fg_mode(self):
        try:
            os.makedirs(CFG_DIR, exist_ok=True)
            with open(FG_MODE_FILE, "w", encoding="utf-8") as f:
                f.write(str(self.settings["fg_mode"]))
        except OSError:
            pass

    def _fg_next(self, appid, mult):
        """Рішення про генерацію кадрів для НАСТУПНОГО запуску (читає обгортка)."""
        path = os.path.join(CFG_DIR, f"fg_{appid}")
        try:
            if mult >= 2:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(str(int(mult)))
            elif os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def _start_notes(self, now):
        """Що обгортка зробила перед стартом: оновлення гри, профіль з бази."""
        notes = []
        upd = _load_json(os.path.join(CACHE_DIR, "game_updated.json"), None)
        if upd and str(upd.get("appid")) == str(self.appid) and int(upd.get("t", 0)) >= self.game_started - 300:
            self.game_update = upd
            notes.append("Гру оновлено — FSR 4 і твіки перевірено/відновлено"
                         + (", графіку перевірю на цьому запуску" if upd.get("retune") else ""))
        com = _load_json(os.path.join(CACHE_DIR, "community_applied.json"), None)
        if com and str(com.get("appid")) == str(self.appid) and int(com.get("t", 0)) >= self.game_started - 300:
            notes.append("Застосовано готовий профіль з бази AutoFPS" +
                         (f": {com['summary']}" if com.get("summary") else ""))
        return notes

    async def share_profile(self, appid=None):
        """Профіль гри для бази: зберігає локально і повертає посилання на GitHub."""
        import profiles_db as PDB
        appid = str(appid or self.appid or getattr(self, "last_appid", "") or "")
        if not appid:
            return {"ok": False, "error": "Немає гри"}
        prof = PDB.export(appid, self.device)
        if not prof:
            return {"ok": False, "error": "Гра ще не підібрана — зіграй кілька разів"}
        os.makedirs(os.path.join(CFG_DIR, "shared"), exist_ok=True)
        _save_json(os.path.join(CFG_DIR, "shared", f"{appid}.json"), prof)
        return {"ok": True, "url": PDB.share_url(prof), "profile": prof}

    # ---------------- живлення: профілі батарея/зарядка, автопідбір TDP

    def _mode(self):
        """Рівень FPS з урахуванням джерела живлення."""
        if self.settings.get("power_profiles", True):
            m = str(self.settings.get("ac_mode" if self.ac else "bat_mode", "same"))
            if m != "same":
                return m
        return str(self.settings["mode"])

    def _vrr(self):
        return bool(self.device.get("vrr")) and self._mode() == "vrr"

    def _tdp_wanted(self):
        """off — не керуємо; max — повна потужність; auto — шукаємо найтихішу сходинку, що тримає FPS."""
        if not self.pw.available:
            return "off"
        a = self.settings.get("auto_tdp", "always")
        if a == "always":
            return "auto"
        if a == "battery":
            return "max" if self.ac else "auto"
        return "off"

    def _tdp_label(self):
        return self.pw.label(self.tdp_cur)

    RESTORE_FILE = os.path.join(CFG_DIR, "restore_on_start.json")

    def _remember_orig(self, kind, snap):
        """Оригінал живлення / частоти GPU — і на диск: якщо плагін перезапуститься посеред
        гри, при старті він поверне систему як було, а не лишить «тихий» профіль чи 1900 МГц."""
        d = _load_json(self.RESTORE_FILE, {})
        d.setdefault(kind, snap)
        _save_json(self.RESTORE_FILE, d)

    def _forget_orig(self, kind):
        d = _load_json(self.RESTORE_FILE, {})
        if d.pop(kind, None) is not None:
            _save_json(self.RESTORE_FILE, d)

    def _restore_after_restart(self):
        d = _load_json(self.RESTORE_FILE, {})
        if not d:
            return
        try:
            if d.get("power") and self.pw.available:
                self.pw.restore(d["power"])
            if d.get("gpu") and self.gc.available:
                self.gc.restore(d["gpu"])
            decky.logger.info(f"після перезапуску плагіна повернуто: {d}")
        except Exception as e:  # noqa: BLE001
            decky.logger.warning(f"відновлення після перезапуску: {e}")
        _save_json(self.RESTORE_FILE, {})

    def _tdp_set(self, i, why=""):
        if self.tdp_orig is None:
            self.tdp_orig = self.pw.snapshot()
            self._remember_orig("power", self.tdp_orig)
        i = max(0, min(len(self.pw.levels) - 1, int(i)))
        if i != self.tdp_cur and self.pw.apply(i):
            decky.logger.info(f"живлення → {self.pw.label(i)} {why}")
        self.tdp_cur = i
        self.tdp_last = time.time()

    def _tdp_restore(self):
        if getattr(self, "tdp_orig", None) is not None and self.pw.available:
            self.pw.restore(self.tdp_orig)
            decky.logger.info(f"живлення повернуто: {self.tdp_orig}")
        self.tdp_orig = None
        self._forget_orig("power")

    def _tdp_saved_key(self):
        out = "vrr" if self._vrr() else self._out_fps()
        return f"{self.device['id']}:{'ac' if self.ac else 'bat'}:{out}"

    def _tdp_begin(self):
        """Рівень FPS зафіксовано — починаємо (або відновлюємо) підбір живлення."""
        want = self._tdp_wanted()
        self.tdp_samples = []
        self.tdp_floor = -1
        self.tdp_ref = None
        if want == "off":
            if self.tdp_state != "off":
                self._tdp_restore()
                self.tdp_cur = None
            self.tdp_state = "off"
            return
        top = len(self.pw.levels) - 1
        if want == "auto" and self.tuning:
            want = "max"          # графіка міряється на повній потужності, тихіше — після підбору
        if want == "max":
            self.tdp_state = "max"
            self._tdp_set(top, "(підбір графіки — повна потужність)" if self.tuning
                          else "(зарядка — повна потужність)")
            return
        self.tdp_key = self._tdp_saved_key()
        saved = (self.games.get(str(self.appid)) or {}).get("tdp", {}).get(self.tdp_key)
        idx = self.pw.index_of(saved) if saved is not None else None
        if idx is not None:
            self.tdp_state = "settled"
            self._tdp_set(idx, "(збережене для цієї гри)")
        else:
            self.tdp_state = "descending"
            self._tdp_set(top, "(старт підбору)")

    def _tdp_save(self):
        if not self.appid or self.tdp_cur is None:
            return
        g = self.games.setdefault(str(self.appid), {})
        g.setdefault("tdp", {})[self.tdp_key or self._tdp_saved_key()] = self.pw.key(self.tdp_cur)
        _save_json(GAMES_FILE, self.games)

    def _tdp_rescue(self):
        """FPS просів, а живлення знижено нами — спершу додаємо потужність, а не знижуємо FPS."""
        if self.tdp_state not in ("descending", "settled") or self.tdp_cur is None \
                or self.tdp_cur >= len(self.pw.levels) - 1 or getattr(self, "hot", False):
            return False
        self.tdp_floor = max(self.tdp_floor, self.tdp_cur)
        self._tdp_set(self.tdp_cur + 1, "(не тримало FPS)")
        if self.tdp_state == "descending":
            self.tdp_state = "settled"
            self._notify(f"AutoFPS: {self._tdp_label()}",
                         f"Найтихіше живлення, що тримає {self._fps_goal_text()}. Запам'ятав для цієї гри")
        self._tdp_save()
        self.tdp_samples = []
        return True

    def _fps_goal_text(self):
        return f"VRR ~{self.tdp_ref:.0f} FPS" if self._vrr() and self.tdp_ref else f"{self._out_fps()} FPS"

    def _tdp_threshold(self):
        """Нижче цього p10 вважаємо, що живлення замало."""
        if self._vrr():
            if not self.tdp_ref:
                return None
            q = C.quality_target(self.settings, self.device)
            return max(self.tdp_ref * 0.88, q * 0.97)     # VRR: до -12% FPS заради тиші
        return self.base * 0.93

    def _thermal_tick(self, now):
        """Перегрів (APU ≥ 87 °C пів хвилини) — тихіша сходинка навіть ціною FPS."""
        if now - getattr(self, "thermal_t", 0) < 5:
            return
        self.thermal_t = now
        self.telemetry = P.telemetry()
        t = self.telemetry.get("temp")
        if t is None:
            return
        self.hot_since = (getattr(self, "hot_since", 0) or now) if t >= 87 else 0
        self.hot = bool(self.hot_since and now - self.hot_since >= 30)
        if self.hot and self.tdp_state in ("descending", "settled") and self.tdp_cur:
            self._tdp_set(self.tdp_cur - 1, f"(перегрів {t} °C)")
            self.tdp_state = "settled"
            self._tdp_save()
            self.hot_since = now
            if not getattr(self, "hot_warned", False):
                self.hot_warned = True
                self._notify(f"AutoFPS: гаряче — {t} °C",
                             f"Знизив живлення до «{self._tdp_label()}». Якщо FPS просяде, вибери нижчий рівень FPS")

    def _tdp_tick(self, now):
        self._thermal_tick(now)
        if self.tdp_state == "off" or self.phase not in ("locked", "manual"):
            return
        if now - getattr(self, "tdp_check_t", 0) > 30 and self.tdp_cur is not None:
            # Steam (або гравець у швидкому меню) міг змінити профіль — повертаємо наш рівень
            self.tdp_check_t = now
            lv = self.pw.levels[self.tdp_cur]
            if "profile" in lv and self.pw.current_profile() not in (None, lv["profile"]):
                self.pw.apply(self.tdp_cur)
                decky.logger.info(f"живлення перепідтверджено: {self.pw.label(self.tdp_cur)}")
        if self.tdp_state == "max":
            return
        self.tdp_samples = [x for x in self.tdp_samples if now - x[0] <= 12]
        if now - self.tdp_last < 12 or len(self.tdp_samples) < 10 or now - self.tdp_samples[0][0] < 10:
            return
        p10 = percentile([x[1] for x in self.tdp_samples], 0.10)
        self.tdp_samples = []
        ok, _med, _gpu = self._perf_window(now)
        if not ok:
            return                              # меню, завантаження, перші секунди — не рішення
        if self._vrr() and self.tdp_ref is None:
            self.tdp_ref = p10                 # VRR: скільки гра дає на повному живленні
            return
        if self.gpu_state in ("ref", "descending"):
            return                             # зараз підбирається частота GPU
        thr = self._tdp_threshold()
        if thr and p10 < thr:
            self.tdp_bad = getattr(self, "tdp_bad", 0) + 1
            if self.tdp_bad >= 2:               # одне вікно — може бути важча сцена
                self.tdp_bad = 0
                if not self._gpu_rescue("FPS просів"):
                    self._tdp_rescue()
            return
        self.tdp_bad = 0
        if self.tdp_state == "descending":
            nxt = self.tdp_cur - 1
            if nxt <= self.tdp_floor or nxt < 0:
                self.tdp_state = "settled"
                self._tdp_save()
                self._notify(f"AutoFPS: {self._tdp_label()}",
                             f"Гра тримає {self._fps_goal_text()} навіть на найтихішому живленні")
                return
            self._tdp_set(nxt, f"(тримає {p10:.0f} FPS)")

    def _power_retry(self, now):
        """steamos-manager після завантаження системи може відповісти не одразу — тоді плагін
        лишався без керування живленням до перезапуску. Пробуємо ще раз (перші 10 хв)."""
        if (self.pw.available and self.gc.available) or now - self.started_t > 600 \
                or now - getattr(self, "pw_retry_t", 0) < 30:
            return
        self.pw_retry_t = now
        if not self.gc.available:
            gc = P.GpuClock(self.pw.tdp, decky.logger.info)
            if gc.available:
                self.gc = gc
                decky.logger.info(f"частота GPU (повторна спроба): {gc.min}–{gc.max} МГц")
        if self.pw.available:
            return
        pw = P.PowerCtl(self.device, decky.logger.info)
        if not pw.available:
            return
        self.pw = pw
        decky.logger.info(f"живлення (повторна спроба): {pw.kind} "
                          f"[{', '.join(pw.label(i) for i in range(len(pw.levels)))}]")
        if self.appid is not None and self.phase in ("locked", "manual"):
            self._tdp_begin()

    def _power_tick(self, now):
        self._power_retry(now)
        if now - self.ac_checked < 5:
            return
        self.ac_checked = now
        ac = P.on_ac()
        if ac == self.ac:
            return
        self.ac = ac
        if self.appid is None:
            return
        src = "зарядки" if ac else "батареї"
        self._notify(f"AutoFPS: живлення від {src}",
                     f"Профіль {'зарядки' if ac else 'батареї'}: FPS {self._mode() if self._mode() != 'auto' else 'авто'}"
                     + (", TDP підбирається" if self._tdp_wanted() == "auto" else
                        ", повна потужність" if self._tdp_wanted() == "max" else ""))
        if self.phase in ("locked", "manual"):
            self._tdp_begin()   # ручний рівень FPS перезастосується в _tick сам

    def _reset_measurements(self, note):
        """Заміри FPS були хибні — скидаємо підбір графіки й живлення (крім профілів з бази)."""
        tune = _load_json(TUNE_FILE, {})
        for e in tune.values():
            if isinstance(e, dict) and not e.get("from_community"):
                for k in ("last", "history", "failed", "ok", "converged", "ineffective",
                          "native_result", "lowres_done", "track"):
                    e.pop(k, None)
                e["note"] = note
        _save_json(TUNE_FILE, tune)
        for g in self.games.values():
            if isinstance(g, dict):
                g.pop("tdp", None)
        _save_json(GAMES_FILE, self.games)
        self.tdp_key = None
        decky.logger.info(f"заміри скинуто: {note}")

    def _counts_generated(self):
        """True — FPS з MangoHud треба ділити на множник (налаштування fps_counts_generated)."""
        return self.sfg >= 2 and bool(self.settings["fps_counts_generated"])

    # ---------------- частота GPU: найнижча фіксована, з якою FPS і кадри рівні
    #
    # Після підбору живлення: замір на авто-частоті (еталон рівності кадрів), потім фіксуємо
    # частоту від максимуму вниз по 200 МГц, поки гра тримає ліміт і p5 FPS не гірший за еталон.
    # Перша, що не тримає, — повертаємо останню робочу й запам'ятовуємо (гра + ліміт + живлення).

    GPU_STEP = 100
    GPU_WIN_S = 12
    # Legion Go S (SteamOS 3.9.5): ручна частота GPU вище ~2300 МГц «душить» процесор до
    # 1397 МГц (ValveSoftware/SteamOS#2995; на цьому Legion: 2500 МГц → CPU 1397, FPS 38 → 21).
    # Важкі ігри на «авто» самі тримають 1700–2300 МГц (LEGO Batman: ~1930) — у цих межах і шукаємо.
    GPU_CPU_STARVED = 1450    # МГц процесора: нижче при ручній частоті — крок невдалий

    def _gpu_range(self):
        lo, hi = self.gc.min, self.gc.max
        dr = self.device.get("gpu_manual_range")
        if dr:
            lo, hi = max(lo, dr[0]), min(hi, dr[1])
        return lo, hi

    def _gpu_hw(self, now):
        """(частота GPU під навантаженням, частота CPU, навантаження GPU) — медіани вікна."""
        w = [x for x in self.gpu_hw if now - x[0] <= self.GPU_WIN_S]
        self.gpu_hw = w
        loaded = [x[1] for x in w if x[1] and x[3] is not None and x[3] >= 85]
        cpus = [x[2] for x in w if x[2]]
        loads = [x[3] for x in w if x[3] is not None]
        med = lambda v: percentile(v, 0.5) if v else None  # noqa: E731
        return med(loaded), med(cpus), med(loads)

    def _gpu_wanted(self):
        return (self.gc.available and self.settings.get("auto_gpu_clock", True)
                and self.phase in ("locked", "manual") and not self.tuning
                and self.calib is None and self.tdp_state != "descending")

    def _gpu_key(self):
        lvl = self.pw.key(self.tdp_cur) if (self.pw.available and self.tdp_cur is not None) else "-"
        return f"{self._tdp_saved_key()}|{lvl}"

    def _gpu_set(self, mhz, why=""):
        if self.gpu_orig is None:
            self.gpu_orig = self.gc.snapshot()
            self._remember_orig("gpu", self.gpu_orig)
        if mhz is None:
            self.gc.set_auto()
            decky.logger.info(f"GPU → авто {why}")
        elif mhz != self.gpu_cur and self.gc.set_clock(mhz):
            decky.logger.info(f"GPU → {mhz} МГц {why}")
        self.gpu_cur = mhz
        self.gpu_samples, self.gpu_t, self.gpu_bad = [], time.time(), 0

    def _gpu_restore(self):
        if self.gpu_orig is not None and self.gc.available:
            self.gc.restore(self.gpu_orig)
            decky.logger.info(f"частоту GPU повернуто: {self.gpu_orig}")
        self.gpu_orig = None
        self._forget_orig("gpu")
        self.gpu_cur = None
        self.gpu_state = "off"

    def _gpu_save(self):
        if not self.appid or not self.gpu_key:
            return
        g = self.games.setdefault(str(self.appid), {})
        g.setdefault("gpu", {})[self.gpu_key] = self.gpu_cur or "auto"
        _save_json(GAMES_FILE, self.games)

    def _gpu_threshold(self):
        ref = self.gpu_ref[0] if self.gpu_ref else None
        if self._vrr():
            if not ref:
                return None
            # VRR: не гірше ніж на «авто» (−5% — шум між сценами)
            return ref * 0.95
        thr = self.base * 0.93
        if ref:
            # гра не дотягує до ліміту (впирається в GPU) — не гірше, ніж на «авто»
            thr = min(thr, ref * 0.95)
        return thr

    def _gpu_rescue(self, why):
        """Просідання при зниженій частоті — спершу +100 МГц (живлення не чіпаємо)."""
        if self.gpu_state != "settled" or self.gpu_cur is None:
            return False
        _lo, hi = self._gpu_range()
        if self.gpu_cur >= hi:
            self._gpu_set(None, f"({why} навіть на {hi} МГц)")
        else:
            self._gpu_set(min(hi, self.gpu_cur + self.GPU_STEP), f"({why})")
        self._gpu_save()
        self.tdp_samples = []
        return True

    def _gpu_tick(self, now):
        if not self._gpu_wanted():
            if self.gpu_state != "off" or self.gpu_cur is not None:
                if self.gpu_cur is not None:
                    self._gpu_set(None, "(підбір частоти призупинено)")
                self.gpu_state = "off"
            return
        lo, hi = self._gpu_range()
        key = self._gpu_key()
        if self.gpu_state == "off" or key != self.gpu_key:
            self.gpu_key = key
            g = self.games.get(str(self.appid)) or {}
            saved = g.get("gpu", {}).get(key)
            if saved and saved != "auto" and not lo <= int(saved) <= hi:
                saved = None                # старе збереження поза безпечним діапазоном
            if not saved and (g.get("gpu_fail", 0) >= 2 or g.get("gpu_starved")):
                # у цій грі фіксована частота вже не тримала (або душила CPU) — не мучимо гру
                # новими спробами на кожній іншій сходинці графіки/живлення
                saved = "auto"
            if saved:
                self.gpu_state = "settled"
                self._gpu_set(None if saved == "auto" else int(saved), "(збережене для цієї гри)")
            else:
                self.gpu_state, self.gpu_ref, self.gpu_ok, self.gpu_ref_clk = "ref", None, None, None
                self.gpu_ref_w = []
                self._gpu_set(None, "(еталон на авто-частоті)")
            return
        self.gpu_samples = [x for x in self.gpu_samples if now - x[0] <= self.GPU_WIN_S]
        if now - self.gpu_t < self.GPU_WIN_S or len(self.gpu_samples) < 20:
            return
        fps = [x[1] for x in self.gpu_samples]
        clk, cpu, load = self._gpu_hw(now)
        self.gpu_samples, self.gpu_t = [], now
        ok, _med, _g = self._perf_window(now, self.GPU_WIN_S)
        if not ok:
            return                              # меню, завантаження, перші секунди — пропускаємо
        p10, p5 = percentile(fps, 0.10), percentile(fps, 0.05)
        if self.gpu_state == "ref":
            if load is None or load < 80 or not clk:
                return                          # еталон — лише справжня гра з навантаженим GPU
            self.gpu_ref_w.append((p10, p5, clk))
            if len(self.gpu_ref_w) < 3:
                return                          # еталон — медіана трьох вікон (~36 с), не одна сцена
            p10 = percentile([w[0] for w in self.gpu_ref_w], 0.5)
            p5 = percentile([w[1] for w in self.gpu_ref_w], 0.5)
            clk = percentile([w[2] for w in self.gpu_ref_w], 0.5)
            self.gpu_ref = (p10, p5)
            self.gpu_ref_clk = clk
            # старт — та частота, яку GPU сам тримав під навантаженням (та сама продуктивність,
            # але без «стрибків» частоти й без задушеного процесора), не вище безпечної межі
            start = hi if not clk else max(lo, min(hi, int(clk // 50 * 50)))
            self.gpu_state = "descending"
            self._gpu_set(start, f"(старт: на авто GPU тримав {clk:.0f} МГц, p10 {p10:.0f})" if clk
                          else f"(старт підбору, p10 {p10:.0f})")
            return
        thr = self._gpu_threshold()
        ref_p5 = self.gpu_ref[1] if self.gpu_ref else p5
        holds = (thr is None or p10 >= thr) and p5 >= min(ref_p5, thr or ref_p5) * 0.95
        starved = bool(cpu and cpu <= self.GPU_CPU_STARVED and (load is None or load < 85))
        if starved:
            holds = False
        detail = f"p10 {p10:.0f}, p5 {p5:.0f}" + (f", CPU {cpu:.0f} МГц" if cpu else "")
        if self.gpu_state == "descending":
            if not holds and self.gpu_bad < 1:
                # одне погане вікно — може бути важча сцена; перевіряємо ще раз на тій самій частоті
                self.gpu_bad += 1
                return
            if holds:
                self.gpu_bad = 0
                self.gpu_ok = self.gpu_cur
                nxt = self.gpu_cur - self.GPU_STEP
                if nxt < lo:
                    self.gpu_state = "settled"
                    self._gpu_save()
                    self._notify(f"AutoFPS: GPU {self.gpu_cur} МГц",
                                 f"Навіть на {self.gpu_cur} МГц гра тримає {self._fps_goal_text()} рівно")
                    return
                self._gpu_set(nxt, f"(тримає: {detail})")
                return
            self.gpu_state = "settled"
            if self.gpu_ok:
                self._gpu_set(self.gpu_ok, f"(на {self.gpu_cur} не тримало: {detail})")
                ref = f" (на авто тримав ~{self.gpu_ref_clk:.0f})" if self.gpu_ref_clk else ""
                msg = (f"Найнижча частота GPU, з якою кадри рівні: {self.gpu_ok} МГц{ref}. "
                       "Менше споживання й рівніший кадр. Запам'ятав для цієї гри")
            else:
                self._gpu_set(None, f"(фіксована частота не тримає — лишаю авто: {detail})")
                gg = self.games.setdefault(str(self.appid), {})
                gg["gpu_fail"] = gg.get("gpu_fail", 0) + 1
                if starved:
                    gg["gpu_starved"] = True
                msg = ("Процесор задихався при фіксованій частоті — лишаю авто" if starved
                       else "Гра потребує повної частоти — лишаю авто")
            self._gpu_save()
            self._notify(f"AutoFPS: GPU {self.gpu_cur or 'авто'}" + (" МГц" if self.gpu_cur else ""), msg)
            return
        # settled: два вікна поспіль не тримає — додаємо частоту
        self.gpu_bad = self.gpu_bad + 1 if not holds else 0
        if self.gpu_bad >= 2:
            self._gpu_rescue("процесор задихається" if starved else "кадри нерівні")

    # ---------------- графіку змінено в меню гри посеред сесії

    def _menu_values(self, e):
        """Поточні значення у файлі налаштувань гри (лише ті, що плагін записував)."""
        import autotune as AT
        cfg = e.get("config")
        if not cfg or not os.path.isfile(cfg):
            return None
        eng = str(e.get("engine", ""))
        if eng == "unreal":
            return AT.read_ini_section(cfg, "ScalabilityGroups")
        if eng.startswith("generic:") and e.get("generic_fmt"):
            import generic_settings as G
            return dict(G.FORMATS[e["generic_fmt"]](cfg).items())
        return None

    SETTINGS_NAMES = (b"ShadowQuality", b"TextureQuality", b"EffectsQuality", b"FoliageQuality",
                      b"PostProcessQuality", b"ViewDistance", b"AntiAliasingQuality",
                      b"ReflectionQuality", b"ShadingQuality", b"GlobalIlluminationQuality")

    def _find_settings_sources(self, e):
        """Файли гри (текст, бінарні, UTF-16), у яких записані ≥3 налаштування якості —
        кандидати на справжнє сховище графіки (крім файлу, який ми вже змінюємо)."""
        cfg = e.get("config") or ""
        roots = []
        if "/Saved/" in cfg:
            roots.append(cfg.split("/Saved/")[0] + "/Saved")
        user = cfg.split("/drive_c/users/steamuser/")[0] + "/drive_c/users/steamuser" if "/steamuser/" in cfg else None
        if user:
            roots += [os.path.join(user, d) for d in ("AppData/Local", "AppData/Roaming",
                                                      "AppData/LocalLow", "Documents", "Saved Games")]
        if e.get("install"):
            roots.append(e["install"])
        names16 = tuple(n.decode().encode("utf-16-le") for n in self.SETTINGS_NAMES)
        found, seen, t0 = [], set(), time.time()
        for root in roots:
            for cur, dirs, files in os.walk(root):
                if time.time() - t0 > 8:
                    break
                dirs[:] = [d for d in dirs if not re.search(r"(?i)cache|shader|logs?$|crash|temp|nvidia|"
                                                              r"microsoft|webcache|d3dscache|binaries|content|paks", d)]
                for fn in files:
                    p = os.path.join(cur, fn)
                    if p in seen or p == cfg or fn.endswith((".autofps.bak", ".log", ".pak", ".utoc", ".ucas")):
                        continue
                    seen.add(p)
                    try:
                        if os.path.getsize(p) > 2 * 1024 * 1024:
                            continue
                        with open(p, "rb") as f:
                            data = f.read()
                    except OSError:
                        continue
                    hits = sum(1 for n in self.SETTINGS_NAMES if n in data) + \
                        sum(1 for n in names16 if n in data)
                    if hits >= 3:
                        found.append(p)
        decky.logger.info(f"пошук сховища налаштувань: {found}")
        return found

    def _menu_tick(self, now):
        """Гравець змінив графіку в меню, поки гра запущена: гра вже застосувала її, а плагін
        змінює налаштування лише перед стартом. Кажемо про це й зупиняємо замір — інакше FPS
        «чужих» налаштувань записався б як результат сходинки плагіна."""
        if self.menu_changed or now - self.menu_t < 10 or not self.settings["autotune"]:
            return
        self.menu_t = now
        e = self._tune_entry()
        if not e or not e.get("applied_values") or e.get("disabled") or e.get("user_override"):
            return
        try:
            mt = os.path.getmtime(e["config"])
        except (OSError, KeyError):
            return
        if self.menu_mtime is None:
            self.menu_mtime = mt
            return
        if mt == self.menu_mtime:
            return
        self.menu_mtime = mt
        try:
            cur = self._menu_values(e)
            applied = {k: v for k, v in e["applied_values"].items() if not k.startswith("x.")}
            if cur is None:
                return
            diff = [k for k, v in applied.items() if k in cur and str(cur[k]).strip() != str(v).strip()]
            # гра сама іноді поправляє одне значення (LEGO Batman: пост-обробка 2 → 3) —
            # ручна зміна в меню — це щонайменше два налаштування
            if len(diff) < 2:
                return
        except Exception as ex:  # noqa: BLE001
            decky.logger.warning(f"перевірка меню гри: {ex}")
            return
        self.menu_changed = True
        was_tuning, self.tuning = self.tuning, False
        self.tm_raw, self.tm_windows = [], []
        lvl = int(e.get("applied_level", e.get("level", 0))) + 1
        if now - self.game_started < 75:
            # так швидко в меню не дійти — гра сама перезаписала файл з іншого джерела
            decky.logger.warning(f"гра перезаписала налаштування при старті: {diff}")
            found = self._find_settings_sources(e)
            self._modify_tune(self.appid, lambda x: x.update(settings_sources=found))
            if found:
                body = ("Гра відновила графіку з іншого місця. Знайшов, де вона її тримає: "
                        + ", ".join(os.path.basename(f) for f in found[:3])
                        + ". З наступного запуску записуватиму й туди")
            else:
                body = ("Гра відновила графіку з іншого місця, і я його не знайшов. "
                        "Збери діагностику й надішли Claude")
            self._notify("AutoFPS: гра перезаписала графіку при запуску",
                         body + ". Замір графіки в цій сесії зупинено")
            return
        if self.settings.get("respect_manual_graphics"):
            body = ("Залишу твої налаштування й далі графіку не чіпатиму "
                    "(увімкнено «Залишати мої ручні налаштування»)")
        else:
            body = (f"Гра вже застосувала їх, а поки вона запущена, AutoFPS їх не змінює. "
                    f"Після перезапуску поверну підібрану сходинку {lvl}/{e.get('steps', '?')}. "
                    "Хочеш грати на своїх — увімкни «Залишати мої ручні налаштування» на вкладці «Графіка»")
        if was_tuning:
            body += ". Замір графіки в цій сесії зупинено"
        self._notify("AutoFPS: графіку змінено в меню гри", body)

    # ---------------- фоновий замір для підбору графіки
    #
    # Раніше крок підбору міряв 20 с без ліміту й лише після кнопки або 3 хв гри — у коротких
    # сесіях заміру не було зовсім і підбір стояв. Тепер: усю сесію збираємо 10-секундні вікна,
    # оцінюємо «скільки гра тягне» і кожні 30 с зберігаємо в tune.json (вихід з гри нічого не губить).

    TM_SKIP_S = 45          # логотипи; завантаження й меню далі відсіюються фільтром вікон
    TM_MIN_WINDOWS = 4      # ≥ 40 с чистої гри до першого запису
    TM_EXIT_WINDOWS = 3     # вихід з гри раніше — зберігаємо, якщо є хоч 30 с чистої гри
    TM_FORECAST_WINDOWS = 6 # ≥ 60 с — лише тоді радимо перезапуск

    def _tm_cap(self):
        """Скільки справжніх FPS зараз дозволяє ліміт (None — без ліміту)."""
        if self.phase == "probe":
            return None
        if self.phase in ("manual", "locked"):
            if self._vrr():
                return self.device["max_hz"] / max(1, self.mult)
            return float(self.base) if self.base else None
        return None

    def _tm_tick(self, now):
        """Вікна по 10 с. Беремо лише «чисту гру»: GPU завантажений (≥ 80%) або гра впирається
        в ліміт; без сильних ривків. Завантаження, заставки, меню й компіляція шейдерів
        (FPS падає до одиниць на десятки секунд) — відкидаються, а з решти береться медіана."""
        if not self.tuning or self.calib is not None:
            self.tm_raw = []
            return
        if now - self.game_started < self.TM_SKIP_S:
            self.tm_raw = []
            return
        if now - self.tm_t < 10:
            return
        self.tm_t = now
        raw, self.tm_raw = self.tm_raw, []
        if len(raw) < 20:
            return
        fps = [x[1] for x in raw]
        gpus = [x[2] for x in raw if x[2] is not None]
        gpu = sum(gpus) / len(gpus) if gpus else None
        p10, med = percentile(fps, 0.10), percentile(fps, 0.5)
        cap = self._tm_cap()
        capped = bool(cap and med >= cap * 0.95)
        if not capped and gpu is not None and gpu < 80:
            return                                   # меню / завантаження / процесор
        if cap and med > cap * 1.15:
            # вище ліміту: меню без обмеження FPS — або ліміт узагалі не діє (шар Lossless).
            # Якщо так уже 2 хв гри з завантаженим GPU — це гра, і вона тягне більше за ліміт
            if gpu is not None and gpu >= 80:
                self.tm_over.append(min(p10, cap * 1.6))
                if len(self.tm_over) >= 12 and len(self.tm_windows) < self.TM_MIN_WINDOWS \
                        and self.tm_saved_n == 0:
                    value = percentile(self.tm_over, 0.25)
                    self._record_tune(value)
                    self.tm_saved_n = len(self.tm_over)
                    decky.logger.info(f"замір вище ліміту (ліміт не діє?): {value:.1f} FPS")
                    self._tm_forecast(value)
            return
        if p10 < med * 0.5:
            return                                   # ривки: шейдери, підвантаження
        if capped:
            # тримає ліміт: запас оцінюємо за навантаженням GPU (99% — запасу немає)
            est = p10 * (min(1.8, 97.0 / max(gpu, 40.0)) if gpu is not None else 1.0)
        else:
            est = p10
        self.tm_windows.append((round(med, 1), round(est, 1)))
        del self.tm_windows[:-60]
        # провали відносно звичного рівня цієї сесії (перехід між рівнями, заставка) — геть
        # орієнтир — верхня чверть вікон: довгі провали (завантаження рівня, 3–10 FPS
        # десятки секунд) не стають «нормою», навіть якщо на початку їх половина
        typical = percentile([w[0] for w in self.tm_windows], 0.75)
        good = [w[1] for w in self.tm_windows if 0.6 * typical <= w[0] <= 1.6 * typical]
        n = len(good)
        if n >= self.TM_EXIT_WINDOWS:
            self.tm_pending = (percentile(good, 0.5), n)
        if n < self.TM_MIN_WINDOWS or (n - self.tm_saved_n < 3 and self.tm_saved_n):
            return
        value = percentile(good, 0.5)
        self._record_tune(value)
        self.tm_saved_n = n
        self._tm_forecast(value)

    def _tm_forecast(self, value):
        """Що зміниться при наступному запуску — кажемо одразу, щоб можна було перезапустити гру."""
        import copy
        import autotune as AT
        entry = _load_json(TUNE_FILE, {}).get(self._tune_key())
        if not entry or not entry.get("steps"):
            return
        e = copy.deepcopy(entry)
        cur = int(e.get("level", 0))
        target = C.quality_target(self.settings, self.device)
        try:
            new, note = AT.decide(e, target, int(e["steps"]))
        except Exception as ex:  # noqa: BLE001
            decky.logger.warning(f"прогноз графіки: {ex}")
            return
        if self.tm_saved_n < self.TM_FORECAST_WINDOWS:
            return                                   # ще замало чистої гри для поради
        key = (new, bool(e.get("converged")))
        if key == getattr(self, "tm_forecast", None):
            return
        self.tm_forecast = key
        if new != cur:
            self._notify("AutoFPS: графіку підібрано — перезапусти гру",
                         f"{note}. Гра бере налаштування лише при старті: вийди й запусти знову")
        elif e.get("converged"):
            self._notify("AutoFPS: графіка підібрана", note)
        else:
            self._notify("AutoFPS: графіку заміряно",
                         f"На цих налаштуваннях ≈{value:.0f} FPS (ціль {target}). Уточнюю, поки граєш")

    # ---------------- decision logic

    def _choose(self, p10):
        """Повертає (base, mult) для виміряного p10 нативного FPS."""
        tiers = self.device["tiers"]
        h = float(self.settings["headroom"])
        native = max([t for t in tiers if p10 >= t * h], default=tiers[0])
        fg = self.settings["fg_mode"]
        if self.cur_info.get("opti_fg"):  # кадри генерує OptiScaler
            return native, 1
        if fg == "external":
            # генерацією керує окремий плагін: множник фіксований, підбираємо лише базу
            m = self.sfg
            if m < 2:
                return native, 1
            fit = [t for t in tiers if t <= native and t * m in tiers]
            return (max(fit), m) if fit else (native, 1)
        if fg == "off" or not self._lsfg_cached()["ready"]:
            return native, 1
        mults = [2, 3] if self.settings["fg_allow_3x"] else [2]
        best = (native, 1)
        if fg == "always" or native < tiers[-1]:
            min_base = int(self.settings["fg_min_base"])
            bases = [t for t in tiers if min_base <= t <= native]
            if 45 >= min_base and 45 <= native and 45 not in bases and 90 in tiers:
                bases.append(45)
            for b in bases:
                for m in mults:
                    out = b * m
                    if out in tiers and out > best[0] * best[1]:
                        best = (b, m)
        return best

    # ---------------- автопідбір графіки (дані пише bin/autotune.py)

    def _tune_key(self, appid=None):
        return f"{appid or self.appid}:{self.device['id']}"

    def _tune_entry(self, appid=None):
        return _load_json(TUNE_FILE, {}).get(self._tune_key(appid))

    def _tuning_active(self, entry):
        return bool(entry and self.settings["autotune"]
                    and entry.get("applied_level") is not None
                    and not entry.get("converged") and not entry.get("ineffective")
                    and not entry.get("disabled"))

    def _record_tune(self, p10, lower_only=False):
        """Записує p10 для поточної сходинки — autotune.py вирішить наступний крок."""
        if not self.tuning:
            return
        tune = _load_json(TUNE_FILE, {})
        entry = tune.get(self._tune_key())
        if not entry or entry.get("applied_level") is None:
            return
        target = C.quality_target(self.settings, self.device)
        last = entry.get("last")
        if lower_only and last and last.get("level") == entry["applied_level"]:
            p10 = min(p10, last["p10"])  # важка сцена — враховуємо гірший випадок
        elif lower_only:
            return
        entry["last"] = {"level": entry["applied_level"], "p10": round(p10, 1),
                         "target": target, "t": int(time.time())}
        _save_json(TUNE_FILE, tune)

    def _modify_tune(self, appid, fn):
        tune = _load_json(TUNE_FILE, {})
        entry = tune.get(self._tune_key(appid))
        if not entry:
            return False
        fn(entry)
        _save_json(TUNE_FILE, tune)
        return True

    def _remember(self, base=None, mult=None):
        """Зберігає рішення для гри. mult — множник для НАСТУПНОГО запуску."""
        if not self.appid:
            return
        base = self.base if base is None else base
        mult = self.mult if mult is None else mult
        old = self.games.get(str(self.appid)) or {}
        self.games[str(self.appid)] = {
            **{k: old[k] for k in ("tdp", "gpu", "gpu_fail", "gpu_starved") if k in old},
            "base": base, "mult": mult,
            "native_p10": round(self.native_p10, 1),
            "device": self.device["id"], "updated": int(time.time()),
        }
        _save_json(GAMES_FILE, self.games)
        self._fg_next(self.appid, mult if self.settings["fg_mode"] not in ("off", "external") else 1)

    def _start_probe(self, now, upgrade=False):
        self.phase, self.phase_t, self.samples = "probe", now, []
        if self.tdp_state != "off" and self.pw.available:
            self._tdp_set(len(self.pw.levels) - 1, "(замір без обмежень)")
            self.tdp_state, self.tdp_key = "max", None
        self.last_probe = now
        self.message = "Вимірюю, скільки гра тягне без обмеження…"
        if upgrade:
            self._notify("AutoFPS: є запас", "Перевіряю, чи гра потягне вищий FPS (~20 с)")
        self._apply()

    def _lock(self, base, mult, now, why, detail="", save=True):
        old_out = self._out_fps() if self.phase == "locked" else 0
        self.base, self.mult = base, mult
        self.phase, self.phase_t, self.samples = "locked", now, []
        self.bad_windows = self.idle_windows = 0
        fg = f" + FG ×{mult} → {base * mult}" if mult > 1 else ""
        self.message = f"{why}: {base} FPS{fg}"
        self._apply()
        if self.tdp_key != self._tdp_saved_key() or self.tdp_state == "off":
            self._tdp_begin()
        if save:
            self._remember()
        if base * mult != old_out:
            body = f"Ліміт {base} FPS"
            body += (f", генерація кадрів ×{mult} → {base * mult} FPS на екрані"
                     if mult > 1 else ", без генерації кадрів")
            if detail:
                body = f"{detail}. {body}"
            self._notify(f"AutoFPS: {base * mult} FPS", body)

    def _step_down(self, now, p10):
        tiers = self.device["tiers"]
        lower = [t for t in tiers if t < self.base]
        if not lower:
            self.message = "Гра не тримає навіть 30 FPS — знизьте графіку в грі"
            return
        why = f"Гра не тримала {self.base} FPS (просідання до {p10:.0f})"
        if self.sfg >= 2:
            # генерацію в цій сесії не вимкнути — знижуємо базовий FPS, множник лишаємо
            self._lock(lower[-1], self.mult, now, "Знизив рівень", why)
            return
        # p10 виміряний під лімітом, тож він — нижня оцінка можливостей гри
        base, mult = self._choose(p10)
        if base >= self.base:
            base, mult = lower[-1], 1
        self._apply_decision(base, mult, now, "Знизив рівень", why, p10)

    def _apply_decision(self, base, mult, now, why, detail, p10):
        """Сесія БЕЗ генерації кадрів: зараз — лише нативний рівень,
        а генерація (якщо потрібна) — з наступного запуску."""
        if mult >= 2:
            h = float(self.settings["headroom"])
            native = max([t for t in self.device["tiers"] if p10 >= t * h] or [base])
            self._lock(max(native, base), 1, now, why, detail, save=False)
            self._remember(base=base, mult=mult)
            if self.sfg == 0 and self.cur_info.get("mode", "full") == "full":
                self._notify("AutoFPS: генерація кадрів — з наступного запуску",
                             f"Гра тримає {base} FPS, з генерацією ×{mult} буде {base * mult} FPS. "
                             "Перезапусти гру, щоб увімкнути")
        else:
            self._lock(base, 1, now, why, detail)

    # ---------------- main loop

    async def _loop(self):
        last_check = 0.0
        while True:
            if time.time() - last_check > O.UPDATE_TTL and self.appid is None:
                last_check = time.time()
                try:
                    await self._blocking(self._opti_check_bg, False)
                except Exception as e:  # noqa: BLE001
                    decky.logger.warning(f"OptiScaler check failed: {e}")
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                decky.logger.error(f"tick error: {e}")
            dt = time.monotonic() - t0
            if dt > 0.5:   # tick іде в циклі подій Decky — довгий tick «підвішує» меню
                self.slow_ticks = getattr(self, "slow_ticks", 0) + 1
                decky.logger.warning(f"повільний tick: {dt:.2f} с")
            await asyncio.sleep(max(0.2, 1.0 - dt))

    SETTLE_S = 45          # перші секунди після старту — логотипи/меню/завантаження, не міряємо

    def _perf_window(self, now, span=12.0):
        """Чи останні span с — «чиста гра», придатна для рішень про живлення / частоту GPU.
        Повертає (придатне, медіана FPS, середнє навантаження GPU). Не придатні: перші
        секунди після старту, меню без обмеження FPS (вище ліміту), завантаження (GPU не
        завантажений і FPS у рази нижчий за звичний)."""
        w = [x for x in self.samples if now - x[0] <= span]
        if now - self.game_started < self.SETTLE_S or len(w) < 10:
            return False, None, None
        fps = [x[1] for x in w]
        gpus = [x[2] for x in w if x[2] is not None]
        med = percentile(fps, 0.5)
        gpu = sum(gpus) / len(gpus) if gpus else None
        cap = self._tm_cap()
        if cap and med > cap * 1.15:
            return False, med, gpu                       # меню без ліміту / ліміт не діє
        if med < 15 or percentile(fps, 0.10) < med * 0.5:
            return False, med, gpu                       # екран завантаження / сильні ривки
        typical = getattr(self, "perf_typical", None)
        if gpu is not None and gpu < 70 and typical and med < typical * 0.6:
            return False, med, gpu                       # завантаження / заставка
        if gpu is not None and gpu >= 80:
            self.perf_typical = med if not typical else 0.8 * typical + 0.2 * med
        return True, med, gpu

    def _cap_check(self, now):
        """Ліміт gamescope не діє (гра йде помітно вище ліміту 3 вікна по 10 с поспіль) —
        вмикаємо обмежувач MangoHud. Меню без ліміту — теж марно спалена батарея."""
        if self.mango_cap or not self.settings.get("mango_limiter", True) or now - self.cap_t < 10:
            return
        self.cap_t = now
        cap = self._tm_cap()
        w = [x[1] for x in self.samples if now - x[0] <= 10]
        if not cap or len(w) < 20 or now - self.last_apply < 3:
            return
        if percentile(w, 0.5) > cap * 1.15:
            self.cap_over += 1
        else:
            self.cap_over = 0
        if self.cap_over >= 3 and os.path.isfile(MANGO_LIMIT_FILE):
            self.mango_cap = True
            decky.logger.info(f"ліміт gamescope не діє ({percentile(w, 0.5):.0f} FPS при ліміті "
                              f"{cap:.0f}) — вмикаю обмежувач MangoHud")
            self._apply()

    def _trim(self, now):
        """Буфери замірів — не довше хвилини (сесія може тривати годинами)."""
        keep = 60.0
        self.samples = [x for x in self.samples if now - x[0] <= keep]
        self.tdp_samples = [x for x in self.tdp_samples if now - x[0] <= keep]
        self.gpu_samples = [x for x in self.gpu_samples if now - x[0] <= keep]
        self.gpu_hw = [x for x in self.gpu_hw if now - x[0] <= keep]
        self.tm_raw = [x for x in self.tm_raw if now - x[0] <= keep]

    def _detect_game(self):
        cur = _load_json(CURRENT_FILE, None)
        if not cur:
            return None
        pid = int(cur.get("pid", 0))
        if pid and os.path.exists(f"/proc/{pid}"):
            return cur
        return None

    def _tick(self):
        now = time.time()
        cur = self._detect_game()

        if not cur:
            if self.appid is not None:
                self._opti_selfcheck(now)
                self._report_crash()
                if self.tuning and self.tm_pending and self.tm_saved_n == 0:
                    # коротка сесія: замір ще не записаний — зберігаємо, що встигли
                    self._record_tune(self.tm_pending[0])
                    decky.logger.info(f"замір графіки при виході: {self.tm_pending[0]:.1f} FPS "
                                      f"({self.tm_pending[1]} вікон)")
                self._restore_display()
                self._tdp_restore()
                self._gpu_restore()
                self._reset_game_state()
                self.reader.reset()
            self._power_tick(now)
            return
        self._power_tick(now)

        if str(cur.get("appid")) != str(self.appid) or cur.get("pid") != self.pid:
            self._reset_game_state()
            self.reader.reset()
            self.appid = str(cur.get("appid"))
            self.pid = cur.get("pid")
            self.game_started = float(cur.get("started", now))
            self.phase, self.phase_t = "warmup", now
            self.last_appid = self.appid
            self.cur_info = cur
            self.sfg = int(cur.get("fg", 0) or 0)
            if self.settings["fg_mode"] == "external":
                self.sfg = their_multiplier() if cur.get("ext_fg") else 0
            decky.logger.info(f"game start: {json.dumps(cur)}")
            if self.sfg >= 2:
                self.mult = self.sfg
            if cur.get("ext_fg") and self.settings.get("mango_limiter", True):
                self.mango_cap = True     # з шаром Decky LSFG-VK ліміт gamescope не діє
            if self.settings.get("last_fg_mult") != max(1, self.sfg):
                self.settings["last_fg_mult"] = max(1, self.sfg)   # для цілі підбору графіки
                _save_json(SETTINGS_FILE, self.settings)
            self._mark_app(self.appid, seen=int(now))
            self.tuning = self._tuning_active(self._tune_entry())
            self.message = "Розігрів (завантаження, шейдери)…"
            if self.settings["enabled"]:
                saved = self.games.get(self.appid) or {}
                if self._mode() == "vrr" and self.device.get("vrr"):
                    nxt = f"VRR до {self.device['max_hz']} FPS — без жорсткого ліміту"
                elif self._mode() != "auto":
                    nxt = f"фіксовано {self._mode()} FPS"
                elif saved.get("device") == self.device["id"] and not self.tuning:
                    nxt = "застосую рівень з минулого разу після розігріву (~20 с)"
                else:
                    nxt = "заміряю FPS після розігріву (~20–40 с)"
                if self.settings.get("start_on_button", True) and self._mode() == "auto":
                    nxt = "коли почнеш грати (не в меню), відкрий AutoFPS і натисни «▶ Почати підбір»"
                if self.tuning:
                    nxt += ". Графіка міряється фоном, поки граєш (досить ~1 хв гри)"
                extra = self._mode_report(cur) + self._start_notes(now)
                body = "\n".join([self._graphics_report(), *extra, f"Далі: {nxt}"])
                self._notify("AutoFPS підключено", body)

        if not self.settings["enabled"]:
            if self.phase != "disabled":
                self._restore_display()  # прибрати наш ліміт FPS і FG
                self.phase = "disabled"
            self.message = "Автоматика вимкнена"
            return
        if self.phase == "disabled":
            self.phase, self.phase_t = "warmup", now

        # Підбір за кнопкою: поки гравець у меню/заставці, нічого не міряємо й не обмежуємо
        # Кнопка потрібна лише там, де міряємо FPS гри: «Авто» або крок підбору графіки.
        # Фіксований ліміт / VRR без підбору графіки — ліміт і живлення одразу, без кнопки.
        no_measure = self._mode() != "auto"
        if self.settings.get("start_on_button", True) and not self.user_started and not no_measure:
            for _ in self.reader.read(self.game_started):
                pass                      # вичитуємо лог, щоб не накопичувати кадри меню
            self.reader.hw = []
            self.samples = []
            if self.phase != "waiting":
                self.phase, self.phase_t = "waiting", now
            # фіксований ліміт діє й у меню, поки чекаємо на замір графіки
            fixed = self._mode()
            if fixed.isdigit() and now - self.last_apply > 5:
                m = self.sfg if self.sfg >= 2 else 1
                self.base, self.mult = max(1, int(fixed) // m), m
                self._apply()
            # підбір графіки без кнопки стояв би вічно — через 3 хв гри починаємо самі
            if self.tuning and now - self.phase_t > 180:
                self.user_started = True
                self.phase, self.phase_t = "warmup", now - max(0.0, float(self.settings["warmup_s"]) - 5.0)
                self._notify("AutoFPS: почав підбір графіки сам",
                             "Кнопку «▶ Почати підбір» не натиснуто 3 хв — міряю FPS для підбору графіки")
                return
            self.message = ("Чекаю: коли почнеш грати, натисни «▶ Почати підбір» у меню AutoFPS"
                            + (" (підбір графіки почнеться сам через 3 хв)" if self.tuning else ""))
            return

        for fps, gpu in self.reader.read(self.game_started):
            if self._counts_generated():
                fps = fps / max(2, self.mult)  # MangoHud бачить і згенеровані кадри
            self.samples.append((now, fps, gpu))
            self.tdp_samples.append((now, fps))
            self.gpu_samples.append((now, fps))
            if self.tuning:
                self.tm_raw.append((now, fps, gpu))
        for clk, cpu, load in self.reader.hw:
            self.gpu_hw.append((now, clk, cpu, load))
        self.reader.hw = []
        self._trim(now)
        self._cap_check(now)
        self._tdp_tick(now)
        self._gpu_tick(now)
        self._menu_tick(now)
        self._tm_tick(now)

        # фіксований рівень вибрано вручну
        mode = self._mode()
        if mode == "vrr" and not self.device.get("vrr"):
            mode = "auto"
        if mode != "auto":
            vrr = mode == "vrr"
            base = self.device["max_hz"] if vrr else int(mode)
            # Крок автопідбору графіки при фіксованому FPS: спершу ~20 с замір без обмеження
            # (з обмеженням не видно запасу), потім — вибраний рівень. Без цього підбір стояв.
            if False and self.tuning and not self.manual_measured:   # замінено фоновим заміром (_tm_tick)
                if self.phase not in ("warmup", "probe"):
                    self.phase, self.phase_t = "warmup", now
                if self.phase == "warmup":
                    self.samples = []
                    self.message = "Розігрів перед заміром графіки…"
                    if now - self.phase_t >= float(self.settings["warmup_s"]):
                        self._start_probe(now)
                    return
                if now - self.phase_t < float(self.settings["probe_s"]):
                    return
                fps = [x[1] for x in self.samples]
                self.manual_measured = True
                if len(fps) >= 10:
                    p10 = percentile(fps, 0.10)
                    self._record_tune(p10)
                    self._notify("AutoFPS: графіку заміряно",
                                 f"Гра тримає щонайменше {p10:.0f} FPS на цих налаштуваннях. "
                                 f"Наступного запуску підберу графіку; зараз — фіксовано {base} FPS")
            if self.phase != "manual" or getattr(self, "manual_key", None) != (mode, self.sfg):
                self.manual_key = (mode, self.sfg)
                self.base, self.mult = base, (self.sfg if self.sfg >= 2 else 1)
                # рівень — це FPS на екрані; з генерацією кадрів гра рендерить у mult разів менше
                self.base = max(1, base // self.mult)
                if vrr:
                    self.phase, self.message = "manual", f"VRR: до {base} FPS, частота екрана йде за грою"
                elif self.mult > 1:
                    self.phase, self.message = "manual", f"Вручну: {base} FPS на екрані ({self.base} × FG {self.mult})"
                else:
                    self.phase, self.message = "manual", f"Вручну: {base} FPS"
                self._apply()
                self._tdp_begin()
                if vrr:
                    self._notify(f"AutoFPS: VRR до {base} FPS",
                                 "Без жорсткого ліміту: екран підлаштовується під FPS гри, картинка плавна")
                else:
                    self._notify(f"AutoFPS: {base} FPS", "Фіксований рівень, вибраний вручну")
            elif now - self.last_apply > 5:
                self._apply()
            return

        if self.phase == "manual":
            self.phase, self.phase_t = "warmup", now

        if self.phase == "warmup":
            self.samples = []
            if now - self.phase_t >= float(self.settings["warmup_s"]):
                saved = self.games.get(self.appid)
                if saved and saved.get("device") != self.device["id"]:
                    saved = None
                if self.sfg >= 2:
                    # обгортка ввімкнула генерацію за рішенням минулого запуску
                    if saved:
                        self.native_p10 = saved.get("native_p10", 0)
                        self._lock(int(saved["base"]), self.sfg, now, "Збережений рівень",
                                   f"Генерація кадрів ×{self.sfg} увімкнена при запуску")
                    else:
                        self._start_probe(now)
                elif self.tuning:
                    self._start_probe(now)  # графіку змінено — міряємо заново
                elif saved and int(saved.get("mult", 1)) == 1:
                    self.native_p10 = saved.get("native_p10", 0)
                    self._lock(int(saved["base"]), 1, now,
                               "Збережений рівень", "Рівень з минулого разу")
                else:
                    self._start_probe(now)
            return

        if self.phase == "probe":
            if now - self.phase_t >= float(self.settings["probe_s"]):
                fps = [s[1] for s in self.samples]
                if len(fps) < 10:
                    self.message = ("Немає даних FPS. Перевірте параметр запуску "
                                    "та що MangoHud встановлений")
                    if not self.warned_no_data and self.cur_info.get("mangohud", 1):
                        self.warned_no_data = True
                        self._notify("AutoFPS: немає даних FPS",
                                     "MangoHud не пише лог — FPS не підбирається. "
                                     "Надішли ~/.cache/autofps/wrapper.log")
                    self.phase_t = now
                    return
                self.native_p10 = percentile(fps, 0.10)
                detail = f"Гра тримає щонайменше {self.native_p10:.0f} FPS"
                base, mult = self._choose(self.native_p10)
                if self.sfg >= 2:
                    # генерація вже йде: множник можна змінити на льоту (2↔3), але не вимкнути
                    if mult < 2:
                        mult = self.sfg
                        base = max([t for t in self.device["tiers"]
                                    if t * mult in self.device["tiers"]] or [base])
                        self._lock(base, mult, now, "Підібрано", detail, save=False)
                        self._remember(base=base, mult=1)  # наступного разу — без генерації
                    else:
                        self._lock(base, mult, now, "Підібрано", detail)
                else:
                    self._record_tune(self.native_p10)
                    self._apply_decision(base, mult, now, "Підібрано", detail, self.native_p10)
            return

        if self.phase == "locked":
            # Steam може перезаписати ліміт — підтверджуємо раз на 5 с
            if now - self.last_apply > 5:
                self._apply()
            window = [s for s in self.samples if now - s[0] <= 10]
            if now - self.phase_t < 10 or len(window) < 10:
                return
            self.samples = window
            if now - window[0][0] < 9:
                return
            p10 = percentile([s[1] for s in window], 0.10)
            gpus = [s[2] for s in window if s[2] is not None]
            gpu_avg = sum(gpus) / len(gpus) if gpus else None

            if p10 < self.base * float(self.settings["drop_ratio"]):
                self.bad_windows += 1
            else:
                self.bad_windows = 0
            if self.bad_windows >= 2 and self.gpu_state in ("ref", "descending"):
                self.bad_windows = 0      # просідання від підбору частоти GPU — він сам поверне
                return
            if self.bad_windows >= 2 and (self._gpu_rescue("FPS просів") or self._tdp_rescue()):
                self.bad_windows = 0      # спершу повертаємо потужність, FPS не чіпаємо
                return
            if self.bad_windows >= 2:
                if self.sfg < 2:
                    self._record_tune(p10, lower_only=True)
                self._step_down(now, p10)
                return

            # Запас по GPU — пробуємо вищий рівень (не частіше ніж раз на 3 хв)
            if gpu_avg is not None and gpu_avg < 55 and \
                    self.base * self.mult < self.device["tiers"][-1]:
                self.idle_windows += 1
            else:
                self.idle_windows = 0
            if self.idle_windows >= 6 and now - self.last_probe > 180 and self.sfg < 2:
                self._start_probe(now, upgrade=True)
            self.samples = [s for s in self.samples if now - s[0] <= 1]

    # ---------------- API for frontend

    async def get_status(self):
        last = [s[1] for s in self.samples[-20:]]
        return {
            "device": self.device,
            "settings": self.settings,
            "appid": self.appid,
            "phase": self.phase,
            "base": self.base,
            "mult": self.mult,
            "out": self._out_fps(),
            "mango_cap": self._mango_cap_value() if self.appid else 0,
            "fps_now": round(sum(last) / len(last), 1) if last else None,
            "native_p10": round(self.native_p10, 1),
            "message": self.message,
            "lsfg": self._lsfg_cached(),
            "tune": self._tune_status(),
            "events": list(reversed(getattr(self, "events", []))),
            "launch_option": LAUNCH_OPTION,
            "lsfg_launcher": lsfg_launcher(self.settings.get("fg_tool", "lsfg")),
            "fg_launchers": fg_launchers(),
            "power": {"ac": self.ac, "battery": P.battery_percent(),
                      "tdp_available": self.pw.available, "kind": self.pw.kind,
                      "level": self.pw.label(self.tdp_cur) if self.tdp_cur is not None else None,
                      "levels": len(self.pw.levels), "level_i": self.tdp_cur,
                      "tdp_state": self.tdp_state, "tdp_error": self.pw.error,
                      "telemetry": self.telemetry, "hot": bool(getattr(self, "hot", False)),
                      "mode": self._mode(),
                      "gpu": {"available": self.gc.available, "state": self.gpu_state,
                              "clock": self.gpu_cur, "min": self.gc.min, "max": self.gc.max,
                              "error": self.gc.error}},
            "game_update": getattr(self, "game_update", None),
            "graphics_target": C.quality_target(self.settings, self.device),
        }

    def _lsfg_cached(self):
        """Перевірка файлів раз на 10 с, а не на кожен запит інтерфейсу."""
        now = time.time()
        if now - getattr(self, "_lsfg_t", 0) > 10:
            self._lsfg_t, self._lsfg = now, lsfg_status(self.settings)
        return self._lsfg

    def _last_app(self):
        """Поточна гра, або остання запущена (і після перезапуску плагіна)."""
        a = self.appid or getattr(self, "last_appid", None)
        if not a:
            a = (_load_json(CURRENT_FILE, None) or {}).get("appid")
        return str(a) if a else None

    def _tune_status(self):
        appid = self._last_app()
        if not appid:
            return None
        e = self._tune_entry(appid)
        if not e:
            return None
        state = ("off" if e.get("disabled") else "user" if e.get("user_override")
                 else "done" if e.get("converged")
                 else "cpu" if e.get("ineffective") else "tuning"
                 if e.get("applied_level") is not None else "wait")
        return {"appid": appid, "engine": e.get("engine"), "state": state,
                "level": e.get("level"), "steps": e.get("steps"),
                "note": e.get("note", ""), "summary": e.get("summary", ""),
                "target": e.get("target")}

    async def retune(self, appid=None):
        """Почати підбір графіки цієї гри з нуля (з наступного запуску). Повертає повідомлення."""
        appid = str(appid or self._last_app() or "")
        if not appid:
            return {"ok": False, "message": "Спершу запусти гру з AutoFPS"}

        def fn(e):
            for k in ("history", "failed", "ok", "converged", "ineffective", "last",
                      "user_override", "applied_values", "start_scaled", "track",
                      "native_result", "lowres_done", "from_community",
                      "disabled", "restore_pending"):
                e.pop(k, None)
            e["level"] = self.device["start_level"]
            e["start_from_user"] = True   # старт — від того, що зараз стоїть у меню гри
            e["note"] = "Підбір почнеться з наступного запуску гри — від твоїх налаштувань у меню"
        if not self._modify_tune(appid, fn):
            return {"ok": False, "message": "Цю гру ще не запускали з AutoFPS — підбір почнеться при першому запуску"}
        running = str(self.appid) == appid
        return {"ok": True, "message": ("Перезапусти гру: графіка змінюється лише при старті. " if running else "") +
                "Далі просто грай: FPS міряється фоном, графіка змінюється з кожним запуском, поки не досягне цілі"}

    async def restore_original(self):
        """Повернути оригінальні налаштування гри (при наступному запуску)."""
        appid = self._last_app()

        if appid:   # твіки рушіїв (Engine.ini UE5, boot.config Unity) теж прибрати
            for name in ("ue_tweaks.json", "unity_tweaks.json"):
                uet = os.path.join(CFG_DIR, name)
                d = _load_json(uet, {})
                if str(appid) in d:
                    d[str(appid)]["restore_pending"] = True
                    _save_json(uet, d)

        def fn(e):
            e["restore_pending"] = True
            e["note"] = "Оригінал буде повернуто при наступному запуску"
        return self._modify_tune(appid, fn)

    async def set_setting(self, key, value):
        if key not in DEFAULT_SETTINGS:
            return False
        self.settings[key] = value
        _save_json(SETTINGS_FILE, self.settings)
        if key in ("fg_flow_scale", "fg_performance_mode"):
            write_lsfg(self.settings, max(2, self.mult))
        if key == "fg_mode":
            self._write_fg_mode()
        if key == "mango_limiter" and self.appid:
            if not value:
                self.mango_cap = False
            elif self.cur_info.get("ext_fg"):
                self.mango_cap = True
            write_mango_limit(self._mango_cap_value())
        if key == "bridge_version":   # застосувати одразу, навіть посеред гри (міст перечитує ini)
            for fn in glob.glob(os.path.join(O.games_dir(), "*.json")):
                g = _load_json(fn, {})
                if g.get("enabled") and g.get("method") == "bridge" and g.get("target"):
                    try:
                        O.bridge_set_version(g["target"], self.settings)
                    except Exception as e:  # noqa: BLE001
                        decky.logger.warning(f"bridge version: {e}")
        return True

    async def set_game_resolution(self, appid, res):
        """Вибрана в AutoFPS роздільність гри (Steam ставить її сам; тут — для підбору графіки)."""
        a = str(appid)
        path = os.path.join(CFG_DIR, "resolution.json")
        data = _load_json(path, {})
        if res and res not in ("Default", "Native"):
            data[a] = res
        else:
            data.pop(a, None)
        _save_json(path, data)
        # нова роздільність — графіку й рівень FPS підбираємо заново
        tune = _load_json(TUNE_FILE, {})
        for k in list(tune):
            if k.startswith(f"{a}:"):
                for f in ("history", "failed", "ok", "converged", "ineffective", "last", "start_scaled", "ladder_sig"):
                    tune[k].pop(f, None)
        _save_json(TUNE_FILE, tune)
        self.games.pop(a, None)
        _save_json(GAMES_FILE, self.games)
        return True

    async def start_tuning(self):
        """Кнопка «Почати підбір»: гравець уже в грі — короткий розігрів і замір."""
        if not self.appid:
            return False
        self.user_started = True
        now = time.time()
        self.samples = []
        # розігрів 5 с замість звичайних 20: шейдери меню вже скомпільовано
        self.phase = "warmup"
        self.phase_t = now - max(0.0, float(self.settings["warmup_s"]) - 5.0)
        self.message = "Розігрів 5 с, далі замір FPS…"
        self._notify("AutoFPS: підбір почався", "Грай як звичайно — за ~30 с AutoFPS підбере рівень FPS")
        return True

    async def reprobe(self):
        if self.appid:
            self.games.pop(self.appid, None)
            _save_json(GAMES_FILE, self.games)
            self._start_probe(time.time())
        return True

    def _mark_app(self, appid, enabled=None, seen=None):
        if not appid or str(appid) == "0":
            return
        apps = _load_json(APPS_FILE, {})
        e = apps.setdefault(str(appid), {})
        if enabled is not None:
            e["enabled"] = bool(enabled)
        if seen is not None:
            e["seen"] = seen
        _save_json(APPS_FILE, apps)

    async def set_game_enabled(self, appid, enabled):
        """Фронтенд повідомляє, що змінив параметри запуску гри."""
        self._mark_app(appid, enabled=enabled)
        if enabled:  # нове увімкнення — знову повний режим
            for fn in (f"mode_{appid}", f"fast_{appid}"):
                try:
                    os.remove(os.path.join(CFG_DIR, fn))
                except OSError:
                    pass
        return True

    def _installed_games(self):
        """{appid: {"name", "install"}} з appmanifest усіх бібліотек Steam."""
        skip = re.compile(r"proton|steam linux runtime|steamworks common|steamvr", re.I)
        games = {}
        for lib in _steam_libraries():
            try:
                names = os.listdir(lib)
            except OSError:
                continue
            for fn in names:
                if not (fn.startswith("appmanifest_") and fn.endswith(".acf")):
                    continue
                acf = _read(os.path.join(lib, fn))
                appid = re.search(r'"appid"\s+"(\d+)"', acf)
                name = re.search(r'"name"\s+"([^"]+)"', acf)
                idir = re.search(r'"installdir"\s+"([^"]+)"', acf)
                if appid and name and not skip.search(name.group(1)):
                    games[appid.group(1)] = {
                        "name": name.group(1),
                        "install": os.path.join(lib, "common", idir.group(1)) if idir else ""}
        return games

    async def list_games(self):
        """Встановлені ігри Steam (для кнопки «Увімкнути AutoFPS у грі»)."""
        apps = _load_json(APPS_FILE, {})
        out = []
        for a, g in self._installed_games().items():
            info = apps.get(a, {})
            opti = O.load_game(a)
            out.append({"appid": int(a), "name": g["name"],
                        "enabled": bool(info.get("enabled")),
                        "seen": int(info.get("seen", 0)),
                        "opti": bool(opti.get("enabled"))})
        return sorted(out, key=lambda g: g["name"].lower())

    # ---------------- OptiScaler («DLSS» для AMD)

    async def _blocking(self, fn, *args):
        return await asyncio.get_event_loop().run_in_executor(None, fn, *args)

    def _opti_check_bg(self, force=False):
        st = O.check_latest(force)
        tag = st.get("latest_tag")
        if tag and O.installed_tag() and O.update_available(st) and st.get("notified_tag") != tag:
            st["notified_tag"] = tag
            O.save_state(st)
            self._notify("AutoFPS: оновлення OptiScaler",
                         f"Доступна версія {tag} (зараз {O.installed_tag()}). "
                         "Онови в меню AutoFPS → DLSS (OptiScaler)")
        return st

    async def opti_status(self, appid=None):
        st = O.load_state()
        tool, _ = O.extractor()
        gpu = O.effective_gpu(self.settings)
        res = {
            "gpu": gpu, "installed": O.installed_tag(), "latest": st.get("latest_tag"),
            "latest_name": st.get("latest_name"), "published": st.get("latest_published", ""),
            "update": O.update_available(st), "check_error": st.get("check_error", ""),
            "checked_at": st.get("checked_at", 0), "extractor": tool or "",
            "upscaler_auto": (O.game_upscaler(appid, self.settings, gpu)[1] if appid
                              else O.resolve_upscaler("auto", gpu)[1]),
            "current": O.resolve_upscaler(self.settings.get("opti_upscaler", "auto"), gpu)[1],
            "detected_arch": O.detect_gpu().get("arch"), "game": None,
            "helixsr": O.helixsr_hlx(),
        }
        if appid:
            a = str(appid)
            g = O.load_game(a)
            info = self._installed_games().get(a, {})
            install = g.get("install") or info.get("install", "")
            prof = O.GAME_PROFILES.get(a, {})
            engine = O.detect_engine(install) if install and os.path.isdir(install) else "generic"
            has_dir = bool(install and os.path.isdir(install))
            ffx = O.find_ffx(install) if has_dir else []
            res["game"] = {
                "method": g.get("method", "optiscaler") if g.get("enabled") else "",
                "planned": O.choose_method(a, install, self.settings, gpu) if has_dir else "",
                "native_fsr31": bool(ffx),
                "direct_variant": g.get("direct_variant", ""),
                "direct_confirmed": bool(g.get("direct_confirmed")),
                "direct_tried": g.get("direct_tried", []),
                "enabled": bool(g.get("enabled")), "tag": g.get("tag"), "dll": g.get("dll"),
                "upscaler": g.get("upscaler_label"), "error": g.get("error", ""),
                "target": (g.get("target", "") or "").replace(install, "").lstrip("/") or "корінь гри",
                "engine": O.ENGINE_PROFILES[engine]["label"],
                "unsupported": prof.get("unsupported", ""),
                "anticheat": (O.has_anticheat(install) or "") if install and os.path.isdir(install) else "",
                "check": g.get("check") or None,
                "dll_tried": g.get("dll_tried", []), "dll_ok": g.get("dll_ok", ""),
                "disabled_reason": g.get("disabled_reason", ""),
                "ref": prof.get("ref", ""), "ref_tag": g.get("ref_tag", ""),
                "pd": O.pd_status() if prof.get("ref") == "pd" else None,
                "found": bool(install and os.path.isdir(install)),
            }
        return res

    async def opti_check(self):
        await self._blocking(self._opti_check_bg, True)
        return await self.opti_status()

    async def opti_update(self):
        try:
            tag = await self._blocking(O.download_latest, decky.logger.info)
            self._notify("AutoFPS: OptiScaler оновлено",
                         f"Версія {tag}. Ігри з OptiScaler оновляться самі при наступному запуску")
            return {"ok": True, "tag": tag}
        except Exception as e:  # noqa: BLE001
            decky.logger.error(f"OptiScaler update failed: {e}")
            return {"ok": False, "error": str(e)}

    def _opti_enable_sync(self, appid):
        a = str(appid)
        info = self._installed_games().get(a)
        if not info or not info.get("install"):
            raise RuntimeError("гру не знайдено серед встановлених")
        return O.enable(a, info["install"], info["name"], settings=self.settings,
                        log=decky.logger.info, reset=True)

    async def opti_enable(self, appid):
        try:
            g = await self._blocking(self._opti_enable_sync, appid)
            return {"ok": True, "upscaler": g.get("upscaler_label"), "dll": g.get("dll"),
                    "tag": g.get("tag"), "ref": g.get("ref", ""),
                    "method": g.get("method", "optiscaler")}
        except O.NeedPdPlugin as e:
            return {"ok": False, "need_pd": True, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            decky.logger.error(f"OptiScaler enable failed: {e}")
            return {"ok": False, "error": str(e)}

    async def opti_direct_feedback(self, appid, ok):
        """«Напис FSR 4 був / не було» після гри з «одним файлом»."""
        try:
            g = await self._blocking(O.direct_feedback, str(appid), bool(ok), decky.logger.info)
            return {"ok": True, "method": g.get("method", ""), "upscaler": g.get("upscaler_label", "")}
        except Exception as e:  # noqa: BLE001
            decky.logger.error(f"FSR4 feedback failed: {e}")
            return {"ok": False, "error": str(e)}

    async def opti_pd_scan(self):
        """Пошук PDPerfPlugin.dll у Завантаженнях / на картках пам'яті."""
        try:
            return await self._blocking(O.find_pd_plugin)
        except Exception as e:  # noqa: BLE001
            return {"present": False, "error": str(e)}

    async def opti_pd_import(self, path):
        """Файл, вибраний у Decky (DLL або архів з ним)."""
        try:
            return await self._blocking(O.import_pd_plugin, path)
        except Exception as e:  # noqa: BLE001
            return {"present": False, "error": str(e)}

    async def opti_disable(self, appid):
        try:
            await self._blocking(O.uninstall, str(appid), decky.logger.info)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ---------------- діагностика

    def _diagnostics_text(self):
        out = []
        try:
            out.append("\n===== живлення =====")
            out.append(json.dumps({"kind": self.pw.kind, "levels": [self.pw.label(i) for i in range(len(self.pw.levels))],
                                   "error": self.pw.error, "cli": self.pw.tdp.cli, "state": self.tdp_state,
                                   "level": self.tdp_cur, "ac": self.ac, "telemetry": P.telemetry(),
                                   "gpu": {"available": self.gc.available, "range": [self.gc.min, self.gc.max],
                                           "error": self.gc.error, "state": self.gpu_state,
                                           "clock": self.gpu_cur, "ref": self.gpu_ref,
                                           "now": self.gc.snapshot() if self.gc.available else None},
                                   "props": (self.pw.tdp._run("get-all-properties") or "")[:1500]},
                                  ensure_ascii=False, indent=2))
        except Exception as e:  # noqa: BLE001
            out.append(f"живлення: {e}")

        def sec(title):
            out.append(f"\n===== {title} =====")

        def add(text):
            out.append(text if isinstance(text, str) else json.dumps(text, indent=2, ensure_ascii=False))

        def tail(path, n):
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    return "".join(f.readlines()[-n:])
            except OSError as e:
                return f"(немає: {e.__class__.__name__})"

        def run(cmd):
            try:
                r = subprocess.run(cmd, env=_clean_env(), capture_output=True, text=True, timeout=3)
                return (r.stdout + r.stderr).strip() or f"(код {r.returncode}, без виводу)"
            except Exception as e:  # noqa: BLE001
                return f"(помилка: {e})"

        meta = _load_json(os.path.join(_PLUGIN_DIR, "package.json"), {})
        add(f"AutoFPS diagnostics · версія {meta.get('version', '?')} · "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}")

        sec("Пристрій")
        dmi = {k: _read(f"/sys/class/dmi/id/{k}") for k in
               ("sys_vendor", "product_name", "product_family", "product_version", "board_name")}
        add(dmi)
        add({k: v for k, v in self.device.items()})
        osr = _read("/etc/os-release")
        add("\n".join(l for l in osr.splitlines()
                      if l.split("=")[0] in ("PRETTY_NAME", "VERSION_ID", "BUILD_ID", "VARIANT_ID")))
        add(f"kernel: {_read('/proc/sys/kernel/osrelease')}")
        add(f"python: {sys.version.split()[0]}  uid: {os.getuid()}")

        sec("Інструменти")
        for tool in ("xprop", "mangohud", "gamescope", "python3"):
            add(f"{tool}: {shutil.which(tool) or 'НЕ ЗНАЙДЕНО'}")

        sec("Gamescope (читання атомів)")
        xprop = shutil.which("xprop") or "/usr/bin/xprop"
        add(run([xprop, "-root", "GAMESCOPE_FPS_LIMIT", "GAMESCOPE_DYNAMIC_REFRESH",
                 "GAMESCOPE_FOCUSED_APP"]))

        sec("Фрейм-генерація")
        add(lsfg_status())
        add("Vulkan implicit layers:")
        for d in _layer_dirs():
            try:
                add(f"  {d}: {sorted(os.listdir(d))}")
            except OSError:
                pass
        add("--- конфіг AutoFPS для lsfg-vk ---")
        add(_read(LSFG_CONF) or "(немає)")
        their = _read(os.path.join(HOME, ".config", "lsfg-vk", "conf.toml"))
        add("--- conf.toml Decky LSFG-VK (перші 15 рядків) ---")
        add("\n".join(their.splitlines()[:15]) or "(немає)")

        sec("Налаштування AutoFPS")
        add(self.settings)

        sec("Поточний стан")
        add({"appid": self.appid, "last_appid": getattr(self, "last_appid", None),
             "phase": self.phase, "base": self.base, "mult": self.mult,
             "native_p10": round(self.native_p10, 1), "message": self.message,
             "samples_in_memory": len(self.samples)})
        add("Журнал:")
        for e in getattr(self, "events", []):
            add(f"  {time.strftime('%H:%M:%S', time.localtime(e['t']))} {e['title']} — {e['body']}")
        add("current.json: " + (_read(CURRENT_FILE) or "(немає)"))

        sec("Графіка: самоперевірка (остання гра)")
        try:
            import settings_audit as SA
            aid = str(getattr(self, "last_appid", None) or self.appid or "")
            te = self._tune_entry(aid) if aid else None
            if te and te.get("install"):
                compat = os.path.join(os.path.dirname(os.path.dirname(te["install"])), "compatdata", aid)
                if not os.path.isdir(compat):
                    compat = os.path.join(HOME, ".local/share/Steam/steamapps/compatdata", aid)
                r = SA.audit(aid, te["install"], compat, dict(self.device))
                add({k: r.get(k) for k in ("engine", "config", "sav", "note", "max_at_7", "unmanaged")})
                for k, v in (r.get("steps") or {}).items():
                    add(f"  {k}: {' / '.join(v)}  (сходинки 1/4/7/10)")
            else:
                add("немає даних підбору для останньої гри")
        except Exception as e:  # noqa: BLE001
            add(f"самоперевірка: {e!r}")

        sec("Ігри")
        add({"apps.json": _load_json(APPS_FILE, {}), "games.json": self.games})
        tune = _load_json(TUNE_FILE, {})
        for e in tune.values():
            e.pop("original", None)  # великий і не потрібен
        add({"tune.json": tune})

        sec("Параметри запуску Steam з AutoFPS (localconfig.vdf)")
        found = False
        for vdf in glob.glob(os.path.join(HOME, ".local/share/Steam/userdata/*/config/localconfig.vdf")):
            last_id = None
            for line in _read(vdf).splitlines():
                m = re.match(r'\s*"(\d+)"\s*$', line)
                if m:
                    last_id = m.group(1)
                m = re.match(r'\s*"LaunchOptions"\s+"(.*)"\s*$', line)
                if m and ("autofps" in m.group(1).lower() or "lsfg" in m.group(1).lower()
                          or "mako" in m.group(1).lower()):
                    add(f"  {last_id}: {m.group(1)}")
                    found = True
        if not found:
            add("  (не знайдено; Steam може записати зміни у файл пізніше)")

        sec("OptiScaler")
        add({"state": O.load_state(), "gpu": O.detect_gpu(), "extractor": O.extractor()[0]})
        for fn in sorted(glob.glob(os.path.join(O.games_dir(), "*.json"))):
            g = _load_json(fn, {})
            add(f"  {os.path.basename(fn)}: enabled={g.get('enabled')} tag={g.get('tag')} "
                f"dll={g.get('dll')} engine={g.get('engine')} upscaler={g.get('upscaler')} "
                f"target={g.get('target')} error={g.get('error', '')}")
            chk = g.get("check") or {}
            if chk:
                add(f"     перевірка: {chk.get('status')} — {chk.get('message', '')} | proxy={chk.get('proxy')} "
                    f"fsr={chk.get('fsr_version')} upscalers={chk.get('upscalers')} errors={chk.get('errors')}")
            if g.get("dll_tried") or g.get("disabled_reason"):
                add(f"     спробувано: {g.get('dll_tried')} {g.get('disabled_reason', '')}")
            log_tail = _read(os.path.join(g.get("target", ""), "OptiScaler.log")).splitlines()[-25:]
            if log_tail:
                add("     OptiScaler.log (кінець):\n       " + "\n       ".join(log_tail))
            if g.get("method") == "direct":
                add(f"     FSR 4 «один файл»: {g.get('direct_variant')} файли={g.get('direct_files')} "
                    f"спробувано={g.get('direct_tried', [])} підтверджено={g.get('direct_confirmed')}")
            if g.get("ref"):
                tgt = g.get("target", "")
                add(f"     REFramework: {g.get('ref')} {g.get('ref_tag')} pd_verified={g.get('pd_verified')} "
                    f"файли={sorted(os.listdir(tgt)) if os.path.isdir(tgt) else '-'}"[:1500])
                for rlog in glob.glob(os.path.join(tgt, "*framework_log.txt"))[:1]:
                    rt = _read(rlog).splitlines()[-40:]
                    add(f"     {os.path.basename(rlog)} (кінець):\n       " + "\n       ".join(rt))
                for plog in glob.glob(os.path.join(tgt, "reframework", "plugins", "*.log"))[:2]:
                    add(f"     {os.path.basename(plog)}:\n       " +
                        "\n       ".join(_read(plog).splitlines()[-20:]))

        sec("Режими запуску й падіння")
        add("lsfg_version: " + (_read(LSFG_VERSION_FILE) or "?") + "  fg_mode: " + (_read(FG_MODE_FILE) or "?"))
        add("перевірка DLL: " + (lsfg_setup(self.settings, force=True).get("detail") or "-"))
        for fn in sorted(glob.glob(os.path.join(CFG_DIR, "fg_*"))):
            if os.path.basename(fn) != "fg_mode":
                add(f"  {os.path.basename(fn)}: ×{_read(fn)} (рішення на наступний запуск)")
        for fn in sorted(glob.glob(os.path.join(CFG_DIR, "mode_*"))):
            add(f"  {os.path.basename(fn)}: {_read(fn)}")
        add("last_crash.json: " + (_read(os.path.join(CACHE_DIR, "last_crash.json")) or "(немає)"))
        add(f"повільних tick-ів (>0.5 с): {getattr(self, 'slow_ticks', 0)}")
        add(f"launcher: {LAUNCHER} → " + ("є" if os.path.isfile(LAUNCHER) else "НЕМАЄ")
            + f", plugin dir: {_PLUGIN_DIR}")

        sec("wrapper.log (останні 80 рядків)")
        add(tail(os.path.join(CACHE_DIR, "wrapper.log"), 80))

        sec("Лог MangoHud (останній)")
        try:
            logs = sorted((os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR)
                           if f.endswith(".csv")), key=os.path.getmtime)
        except OSError:
            logs = []
        if logs:
            last = logs[-1]
            add(f"{os.path.basename(last)} · {os.path.getsize(last)} байт · "
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(last)))}"
                f" · всього логів: {len(logs)}")
            lines = _read(last).splitlines()
            add("\n".join(lines[:6] + ["…"] + lines[-15:]))
        else:
            add("(логів немає — MangoHud нічого не записав)")

        sec("Лог Decky для AutoFPS (останні 120 рядків)")
        dlogs = sorted(glob.glob(os.path.join(HOME, "homebrew/logs/AutoFPS/*.log")),
                       key=os.path.getmtime)
        add(tail(dlogs[-1], 120) if dlogs else "(немає)")

        text = "\n".join(out)
        # приватність: домашню теку й номер акаунта Steam прибираємо
        text = text.replace(HOME, "~")
        text = re.sub(r"userdata/\d+", "userdata/<id>", text)
        return text

    async def collect_diagnostics(self):
        path = os.path.join(HOME, "autofps-diagnostics.txt")
        try:
            text = self._diagnostics_text()
            with open(path, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            return {"ok": True, "path": "~/autofps-diagnostics.txt",
                    "size": len(text.encode("utf-8"))}
        except Exception as e:  # noqa: BLE001
            decky.logger.error(f"diagnostics failed: {e}")
            return {"ok": False, "error": str(e)}

    async def forget_all(self):
        self.games = {}
        _save_json(GAMES_FILE, self.games)
        return True
