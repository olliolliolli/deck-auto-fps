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

C.set_home(decky.DECKY_USER_HOME)
HOME = C.HOME
CFG_DIR, CACHE_DIR, LOG_DIR = C.CFG_DIR, C.CACHE_DIR, C.LOG_DIR
CURRENT_FILE, SETTINGS_FILE, GAMES_FILE = C.CURRENT_FILE, C.SETTINGS_FILE, C.GAMES_FILE
TUNE_FILE, LSFG_CONF, PROFILES_DIR = C.TUNE_FILE, C.LSFG_CONF, C.PROFILES_DIR
APPS_FILE = os.path.join(CFG_DIR, "apps.json")  # {appid: {"enabled": bool, "seen": ts}}

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

LSFG_PROFILE = "autofps"


def _lsfg_dll_path():
    """Шлях до Lossless.dll з конфіга Decky LSFG-VK (якщо там вказаний)."""
    text = _read(os.path.join(HOME, ".config", "lsfg-vk", "conf.toml"))
    m = re.search(r'^\s*dll\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else ""


def write_lsfg(settings, multiplier):
    """Власний профіль lsfg-vk у форматі v2 (його ставить актуальний Decky LSFG-VK).
    Обгортка гри вибирає його через LSFGVK_CONFIG + LSFGVK_PROFILE.
    multiplier=1 — без генерації. Файл перечитується на льоту."""
    flow = max(0.25, min(1.0, float(settings["fg_flow_scale"])))
    dll = _find_dll()
    lines = ["version = 2", "", "[global]"]
    if dll:
        lines.append(f"dll = {json.dumps(dll)}")
    lines += [
        "allow_fp16 = true",
        "",
        "[[profile]]  # керується AutoFPS",
        f'name = "{LSFG_PROFILE}"',
        'pacing_mode = "vsync"',
        f"multiplier = {int(max(1, multiplier))}",
        f"flow_scale = {flow:.2f}",
        f"performance_mode = {'true' if settings['fg_performance_mode'] else 'false'}",
        "override_present_mode = true",
        "preserve_swapchain_image_count = false",
    ]
    text = "\n".join(lines) + "\n"
    os.makedirs(CFG_DIR, exist_ok=True)
    if _read(LSFG_CONF) != text.strip():
        with open(LSFG_CONF, "w", encoding="utf-8") as f:
            f.write(text)


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
            if "lsfg" in low or "mako" in low:
                found.append(fn)
                continue
            # запасний варіант: ім'я шару всередині маніфесту
            text = _read(os.path.join(d, fn)).lower()
            if '"name"' in text and ("lsfg" in text or "lossless" in text):
                found.append(fn)
    return found


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


def _find_dll():
    """Lossless.dll: з конфіга Decky LSFG-VK або з бібліотек Steam."""
    dll = _lsfg_dll_path()
    if dll and os.path.isfile(dll):
        return dll
    for lib in _steam_libraries():
        cand = os.path.join(lib, "common", "Lossless Scaling", "Lossless.dll")
        if os.path.isfile(cand):
            return cand
    return ""


def lsfg_status():
    """Детальна перевірка фрейм-генерації: що знайдено і що зробити."""
    layers = _find_layers()
    has_lsfg = any("mako" not in fn.lower() for fn in layers)
    has_mako = any("mako" in fn.lower() for fn in layers) or \
        os.path.exists(os.path.join(HOME, ".local/bin/mako-launch"))
    plugins = _decky_plugins()
    lsfg_plugin = next((p for p in plugins if "lsfg" in p.lower()), None)
    mako_plugin = next((p for p in plugins if "mako" in p.lower()), None)
    dll = _find_dll()

    if has_lsfg and dll:
        hint, ready = "lsfg-vk встановлено, Lossless Scaling знайдено — генерація кадрів готова", True
    elif has_lsfg:
        hint, ready = ("lsfg-vk встановлено, але не знайдено Lossless.dll. Встанови Lossless Scaling "
                       "у Steam (потрібна купівля), потім у Decky LSFG-VK перевір шлях до DLL"), False
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
    return {"ready": ready, "hint": hint, "layers": layers,
            "plugins": [p for p in (lsfg_plugin, mako_plugin) if p], "dll": dll}


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
        write_lsfg(self.settings, 1)
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
        self.message = "Гра не запущена через AutoFPS"

    def _restore_display(self):
        try:
            write_lsfg(self.settings, 1)
            apply_display(self.device, 0, self.device["max_hz"]
                          if self.device["refresh_control"] else None)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- сповіщення

    def _notify(self, title, body):
        """Тост у Steam + запис у журнал плагіна."""
        self.events.append({"t": int(time.time()), "title": title, "body": body})
        del self.events[:-12]
        emit = getattr(decky, "emit", None)
        loop = getattr(self, "loop", None)
        if emit and loop:
            try:
                loop.create_task(emit("autofps_toast", title, body))
            except Exception as e:  # noqa: BLE001
                decky.logger.warning(f"emit failed: {e}")

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
        write_lsfg(self.settings, 1 if self.phase == "probe" else self.mult)
        self.last_apply = time.time()

    # ---------------- decision logic

    def _choose(self, p10):
        """Повертає (base, mult) для виміряного p10 нативного FPS."""
        tiers = self.device["tiers"]
        h = float(self.settings["headroom"])
        native = max([t for t in tiers if p10 >= t * h], default=tiers[0])
        fg = self.settings["fg_mode"]
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

    def _remember(self):
        if self.appid:
            self.games[str(self.appid)] = {
                "base": self.base, "mult": self.mult,
                "native_p10": round(self.native_p10, 1),
                "device": self.device["id"], "updated": int(time.time()),
            }
            _save_json(GAMES_FILE, self.games)

    def _start_probe(self, now, upgrade=False):
        self.phase, self.phase_t, self.samples = "probe", now, []
        self.last_probe = now
        self.message = "Вимірюю, скільки гра тягне без обмеження…"
        if upgrade:
            self._notify("AutoFPS: є запас", "Перевіряю, чи гра потягне вищий FPS (~20 с)")
        self._apply()

    def _lock(self, base, mult, now, why, detail=""):
        old_out = self._out_fps() if self.phase == "locked" else 0
        self.base, self.mult = base, mult
        self.phase, self.phase_t, self.samples = "locked", now, []
        self.bad_windows = self.idle_windows = 0
        fg = f" + FG ×{mult} → {base * mult}" if mult > 1 else ""
        self.message = f"{why}: {base} FPS{fg}"
        self._apply()
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
        # p10 виміряний під лімітом, тож він — нижня оцінка можливостей гри
        base, mult = self._choose(p10)
        if base >= self.base:
            base, mult = lower[-1], self.mult
            if mult > 1 and base * mult not in tiers:
                mult = 1
        self._lock(base, mult, now, "Знизив рівень",
                   f"Гра не тримала {self.base} FPS (просідання до {p10:.0f})")

    # ---------------- main loop

    async def _loop(self):
        while True:
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
            self._mark_app(self.appid, seen=int(now))
            self.tuning = self._tuning_active(self._tune_entry())
            self.message = "Розігрів (завантаження, шейдери)…"
            if self.settings["enabled"]:
                nxt = ("заміряю FPS після розігріву (~20–40 с)" if self.settings["mode"] == "auto"
                       else f"фіксовано {self.settings['mode']} FPS")
                self._notify("AutoFPS підключено", f"{self._graphics_report()}\nДалі: {nxt}")

        if not self.settings["enabled"]:
            if self.phase != "disabled":
                self._restore_display()  # прибрати наш ліміт FPS і FG
                self.phase = "disabled"
            self.message = "Автоматика вимкнена"
            return
        if self.phase == "disabled":
            self.phase, self.phase_t = "warmup", now

        for fps, gpu in self.reader.read(self.game_started):
            if self.mult > 1 and self.phase == "locked" \
                    and self.settings["fps_counts_generated"]:
                fps = fps / self.mult
            self.samples.append((now, fps, gpu))

        # фіксований рівень вибрано вручну
        mode = self.settings["mode"]
        if mode != "auto":
            base = int(mode)
            if self.phase != "manual" or self.base != base:
                self.base, self.mult = base, 1
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
                if self.tuning:
                    self._start_probe(now)  # графіку змінено — міряємо заново
                elif saved and saved.get("device") == self.device["id"]:
                    self.native_p10 = saved.get("native_p10", 0)
                    self._lock(int(saved["base"]), int(saved["mult"]), now,
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
                    if not self.warned_no_data:
                        self.warned_no_data = True
                        self._notify("AutoFPS: немає даних FPS",
                                     "MangoHud не пише лог — FPS не підбирається. "
                                     "Надішли ~/.cache/autofps/wrapper.log")
                    self.phase_t = now
                    return
                self.native_p10 = percentile(fps, 0.10)
                self._record_tune(self.native_p10)
                base, mult = self._choose(self.native_p10)
                self._lock(base, mult, now, "Підібрано",
                           f"Гра тримає щонайменше {self.native_p10:.0f} FPS")
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
                if self.mult == 1:
                    self._record_tune(p10, lower_only=True)
                self._step_down(now, p10)
                return

            # Запас по GPU — пробуємо вищий рівень (не частіше ніж раз на 3 хв)
            if gpu_avg is not None and gpu_avg < 55 and \
                    self.base * self.mult < self.device["tiers"][-1]:
                self.idle_windows += 1
            else:
                self.idle_windows = 0
            if self.idle_windows >= 6 and now - self.last_probe > 180:
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
            "launch_option": f"bash {HOME}/homebrew/plugins/AutoFPS/bin/autofps %command%",
        }

    def _lsfg_cached(self):
        """Перевірка файлів раз на 10 с, а не на кожен запит інтерфейсу."""
        now = time.time()
        if now - getattr(self, "_lsfg_t", 0) > 10:
            self._lsfg_t, self._lsfg = now, lsfg_status()
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
        if key in ("fg_flow_scale", "fg_performance_mode") and self.phase == "locked":
            write_lsfg(self.settings, self.mult)
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
        return True

    async def list_games(self):
        """Встановлені ігри Steam (для кнопки «Увімкнути AutoFPS у грі»)."""
        libs = _steam_libraries()
        skip = re.compile(r"proton|steam linux runtime|steamworks common|steamvr", re.I)
        games = {}
        for lib in libs:
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
                if appid and name and not skip.search(name.group(1)):
                    games[appid.group(1)] = name.group(1)
        apps = _load_json(APPS_FILE, {})
        out = []
        for a, n in games.items():
            info = apps.get(a, {})
            out.append({"appid": int(a), "name": n,
                        "enabled": bool(info.get("enabled")),
                        "seen": int(info.get("seen", 0))})
        return sorted(out, key=lambda g: g["name"].lower())

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
