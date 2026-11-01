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

C.set_home(decky.DECKY_USER_HOME)
HOME = C.HOME
CFG_DIR, CACHE_DIR, LOG_DIR = C.CFG_DIR, C.CACHE_DIR, C.LOG_DIR
CURRENT_FILE, SETTINGS_FILE, GAMES_FILE = C.CURRENT_FILE, C.SETTINGS_FILE, C.GAMES_FILE
TUNE_FILE, LSFG_CONF, PROFILES_DIR = C.TUNE_FILE, C.LSFG_CONF, C.PROFILES_DIR
APPS_FILE = os.path.join(CFG_DIR, "apps.json")
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
    "mode": "auto",            # "auto" або фіксований рівень, напр. "60"
    "headroom": 0.95,          # p10 FPS має бути >= рівень * headroom
    "drop_ratio": 0.88,        # якщо p10 < рівень * drop_ratio — знижуємо
    "warmup_s": 20,            # пропустити завантаження/шейдери
    "probe_s": 20,             # скільки секунд міряти без ліміту
    "fg_mode": "auto",         # "off" | "auto" | "always"
    "fg_min_base": 40,         # мінімальний базовий FPS для фрейм-генерації
    "fg_allow_3x": False,
    "fg_flow_scale": 0.8,
    "fg_performance_mode": True,
    "fps_counts_generated": True,  # MangoHud бачить згенеровані кадри
    "autotune": True,          # автопідбір графіки гри (Unreal/Unity)
    "quality_target": "auto",  # базовий FPS, під який підбирається графіка
    "respect_manual_graphics": False,  # True — ручні зміни в меню гри зупиняють підбір
    "opti_upscaler": "auto",   # auto | fsr31 | xess | fsr4 — чим OptiScaler замінює DLSS
    "opti_fg": False,          # генерація кадрів OptiScaler (тоді lsfg-vk у цій грі вимикається)
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


def apply_display(device, fps_limit, refresh):
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
        self.games = _load_json(GAMES_FILE, {})
        self.reader = LogReader()
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
        self.warned_no_data = False
        self.cur_info = {}
        self.sfg = 0              # множник генерації кадрів, увімкненої обгорткою в цій сесії
        self.message = "Гра не запущена через AutoFPS"

    def _restore_display(self):
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
        if cur.get("opti"):
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
        eng = {"unreal": "Unreal", "unity": "Unity"}.get(e.get("engine"), "")
        if e.get("engine") == "unknown":
            return "Рушій гри не підтримується — графіку не змінюю, лише FPS"
        if e.get("user_override"):
            return "Графіка: твої налаштування з меню гри — не чіпаю"
        if e.get("disabled"):
            return "Графіка: оригінальні налаштування гри"
        if e.get("applied_level") is None:
            return e.get("note") or "Графіку не змінено"
        step = f"сходинка {e['applied_level'] + 1}/{e.get('steps', '?')}"
        verb = "Змінено графіку" if e.get("changed") else "Графіка без змін"
        return f"{verb} ({eng}, {step}): {e.get('summary', '')}. {e.get('note', '')}".strip()

    def _out_fps(self):
        return self.base * self.mult

    def _apply(self):
        out = self._out_fps()
        limit = 0 if self.phase == "probe" else out
        refresh = best_refresh(self.device,
                               self.device["max_hz"] if self.phase == "probe" else out)
        apply_display(self.device, limit, refresh)
        if self.sfg >= 2:  # генерація активна — множник змінюється на льоту (лише >= 2)
            write_lsfg(self.settings, max(2, self.mult))
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

    # ---------------- decision logic

    def _choose(self, p10):
        """Повертає (base, mult) для виміряного p10 нативного FPS."""
        tiers = self.device["tiers"]
        h = float(self.settings["headroom"])
        native = max([t for t in tiers if p10 >= t * h], default=tiers[0])
        fg = self.settings["fg_mode"]
        if self.cur_info.get("opti_fg"):  # кадри генерує OptiScaler
            return native, 1
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
        self.games[str(self.appid)] = {
            "base": base, "mult": mult,
            "native_p10": round(self.native_p10, 1),
            "device": self.device["id"], "updated": int(time.time()),
        }
        _save_json(GAMES_FILE, self.games)
        self._fg_next(self.appid, mult if self.settings["fg_mode"] != "off" else 1)

    def _start_probe(self, now, upgrade=False):
        self.phase, self.phase_t, self.samples = "probe", now, []
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
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                decky.logger.error(f"tick error: {e}")
            await asyncio.sleep(1.0)

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
                self._report_crash()
                self._restore_display()
                self._reset_game_state()
                self.reader.reset()
            return

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
            decky.logger.info(f"game start: {json.dumps(cur)}")
            if self.sfg >= 2:
                self.mult = self.sfg
            self._mark_app(self.appid, seen=int(now))
            self.tuning = self._tuning_active(self._tune_entry())
            self.message = "Розігрів (завантаження, шейдери)…"
            if self.settings["enabled"]:
                saved = self.games.get(self.appid) or {}
                if self.settings["mode"] != "auto":
                    nxt = f"фіксовано {self.settings['mode']} FPS"
                elif saved.get("device") == self.device["id"] and not self.tuning:
                    nxt = "застосую рівень з минулого разу після розігріву (~20 с)"
                else:
                    nxt = "заміряю FPS після розігріву (~20–40 с)"
                extra = self._mode_report(cur)
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

        for fps, gpu in self.reader.read(self.game_started):
            if self.sfg >= 2 and self.settings["fps_counts_generated"]:
                fps = fps / max(2, self.mult)  # MangoHud бачить і згенеровані кадри
            self.samples.append((now, fps, gpu))

        # фіксований рівень вибрано вручну
        mode = self.settings["mode"]
        if mode != "auto":
            base = int(mode)
            if self.phase != "manual" or self.base != base:
                self.base, self.mult = base, (self.sfg if self.sfg >= 2 else 1)
                self.phase, self.message = "manual", f"Вручну: {base} FPS"
                self._apply()
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
            "fps_now": round(sum(last) / len(last), 1) if last else None,
            "native_p10": round(self.native_p10, 1),
            "message": self.message,
            "lsfg": self._lsfg_cached(),
            "tune": self._tune_status(),
            "events": list(reversed(getattr(self, "events", []))),
            "launch_option": LAUNCH_OPTION,
        }

    def _lsfg_cached(self):
        """Перевірка файлів раз на 10 с, а не на кожен запит інтерфейсу."""
        now = time.time()
        if now - getattr(self, "_lsfg_t", 0) > 10:
            self._lsfg_t, self._lsfg = now, lsfg_status(self.settings)
        return self._lsfg

    def _tune_status(self):
        appid = self.appid or getattr(self, "last_appid", None)
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

    async def retune(self):
        """Почати підбір графіки цієї гри з нуля (з наступного запуску)."""
        appid = self.appid or getattr(self, "last_appid", None)

        def fn(e):
            for k in ("history", "failed", "converged", "ineffective", "last",
                      "user_override", "applied_values",
                      "disabled", "restore_pending"):
                e.pop(k, None)
            e["level"] = self.device["start_level"]
            e["note"] = "Підбір почнеться з наступного запуску гри"
        return self._modify_tune(appid, fn)

    async def restore_original(self):
        """Повернути оригінальні налаштування гри (при наступному запуску)."""
        appid = self.appid or getattr(self, "last_appid", None)

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
            try:
                os.remove(os.path.join(CFG_DIR, f"mode_{appid}"))
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
        gpu = O.detect_gpu()
        res = {
            "gpu": gpu, "installed": O.installed_tag(), "latest": st.get("latest_tag"),
            "latest_name": st.get("latest_name"), "published": st.get("latest_published", ""),
            "update": O.update_available(st), "check_error": st.get("check_error", ""),
            "checked_at": st.get("checked_at", 0), "extractor": tool or "",
            "upscaler_auto": O.resolve_upscaler("auto", gpu)[1], "game": None,
        }
        if appid:
            a = str(appid)
            g = O.load_game(a)
            info = self._installed_games().get(a, {})
            install = g.get("install") or info.get("install", "")
            prof = O.GAME_PROFILES.get(a, {})
            engine = O.detect_engine(install) if install and os.path.isdir(install) else "generic"
            res["game"] = {
                "enabled": bool(g.get("enabled")), "tag": g.get("tag"), "dll": g.get("dll"),
                "upscaler": g.get("upscaler_label"), "error": g.get("error", ""),
                "target": (g.get("target", "") or "").replace(install, "").lstrip("/") or "корінь гри",
                "engine": O.ENGINE_PROFILES[engine]["label"],
                "unsupported": prof.get("unsupported", ""),
                "anticheat": (O.has_anticheat(install) or "") if install and os.path.isdir(install) else "",
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
        if not O.installed_tag():
            O.download_latest(decky.logger.info)
        return O.install(a, info["install"], info["name"], settings=self.settings,
                         log=decky.logger.info)

    async def opti_enable(self, appid):
        try:
            g = await self._blocking(self._opti_enable_sync, appid)
            return {"ok": True, "upscaler": g.get("upscaler_label"), "dll": g.get("dll"),
                    "tag": g.get("tag")}
        except Exception as e:  # noqa: BLE001
            decky.logger.error(f"OptiScaler enable failed: {e}")
            return {"ok": False, "error": str(e)}

    async def opti_disable(self, appid):
        try:
            await self._blocking(O.uninstall, str(appid), decky.logger.info)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ---------------- діагностика

    def _diagnostics_text(self):
        out = []

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

        sec("Режими запуску й падіння")
        add("lsfg_version: " + (_read(LSFG_VERSION_FILE) or "?") + "  fg_mode: " + (_read(FG_MODE_FILE) or "?"))
        add("перевірка DLL: " + (lsfg_setup(self.settings, force=True).get("detail") or "-"))
        for fn in sorted(glob.glob(os.path.join(CFG_DIR, "fg_*"))):
            if os.path.basename(fn) != "fg_mode":
                add(f"  {os.path.basename(fn)}: ×{_read(fn)} (рішення на наступний запуск)")
        for fn in sorted(glob.glob(os.path.join(CFG_DIR, "mode_*"))):
            add(f"  {os.path.basename(fn)}: {_read(fn)}")
        add("last_crash.json: " + (_read(os.path.join(CACHE_DIR, "last_crash.json")) or "(немає)"))
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
