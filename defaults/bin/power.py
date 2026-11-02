"""Живлення: джерело (батарея / зарядка) і ліміт TDP через steamos-manager (steamosctl).

Steam сам показує повзунок TDP через той самий steamos-manager, тож ми не потребуємо root
і працюємо на будь-якому пристрої, який підтримує SteamOS (Steam Deck, Legion Go S).
"""
import glob
import os
import shutil
import subprocess


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def on_ac():
    """True — живлення від мережі (зарядка під'єднана)."""
    for d in glob.glob("/sys/class/power_supply/*"):
        typ = _read(os.path.join(d, "type"))
        if typ in ("Mains", "USB", "USB_PD", "USB_C") and _read(os.path.join(d, "online")) == "1":
            return True
    for d in glob.glob("/sys/class/power_supply/BAT*"):
        if _read(os.path.join(d, "status")) in ("Charging", "Full"):
            return True
    has_bat = bool(glob.glob("/sys/class/power_supply/BAT*"))
    return not has_bat          # без батареї (ПК) — вважаємо мережею


def battery_percent():
    for d in glob.glob("/sys/class/power_supply/BAT*"):
        v = _read(os.path.join(d, "capacity"))
        if v.isdigit():
            return int(v)
    return None


def _env():
    env = dict(os.environ)
    for k in ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH"):
        env.pop(k, None)
    uid = os.getuid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    return env


class Tdp:
    """Ліміт TDP (Вт) через steamosctl. available == False — пристрій/система не підтримує."""

    def __init__(self, log=print):
        self.log = log
        self.cli = shutil.which("steamosctl") or ("/usr/bin/steamosctl" if os.path.exists("/usr/bin/steamosctl") else None)
        self.min = self.max = None
        self.available = False
        self.error = ""
        if not self.cli:
            self.error = "немає steamosctl (steamos-manager)"
            return
        lo, hi = self._get("get-tdp-limit-min"), self._get("get-tdp-limit-max")
        cur = self.get()
        if cur is None or not hi:
            self.error = "steamos-manager не керує TDP на цьому пристрої"
            return
        self.min, self.max = max(3, lo or 5), hi
        self.available = True

    def _run(self, *args):
        try:
            p = subprocess.run([self.cli, *args], capture_output=True, text=True, timeout=4, env=_env())
        except (OSError, subprocess.SubprocessError) as e:
            self.error = str(e)
            return None
        if p.returncode != 0:
            self.error = (p.stderr or p.stdout).strip()[:200]
            return None
        return p.stdout.strip()

    def _get(self, cmd):
        out = self._run(cmd)
        if not out:
            return None
        num = "".join(ch for ch in out.split()[-1] if ch.isdigit() or ch == ".")
        try:
            return int(float(num))
        except ValueError:
            return None

    def get(self):
        return self._get("get-tdp-limit")

    def set(self, watts):
        if not self.cli:
            return False
        w = int(round(watts))
        if self.available:
            w = max(self.min, min(self.max, w))
        ok = self._run("set-tdp-limit", str(w)) is not None
        if not ok:
            self.log(f"TDP: не вдалося поставити {w} Вт: {self.error}")
        return ok


# ------------------------------------------------------------------ сходинки живлення

# Lenovo Legion Go S: «custom» (ручний TDP) у прошивці вмикає агресивну таблицю вентилятора —
# пристрій гуде. Тихіше — вбудовані профілі Lenovo, у кожного своя спокійна крива вентилятора.
LOUD_PROFILES = {"custom", "max-power", "extreme"}
PROFILE_ORDER = ["low-power", "quiet", "power-saver", "balanced", "balanced-performance", "performance"]
PROFILE_LABEL = {"low-power": "Тихий (Lenovo, ≈8 Вт)", "quiet": "Тихий", "power-saver": "Економний",
                 "balanced": "Збалансований (≈15 Вт)", "balanced-performance": "Збалансований+",
                 "performance": "Продуктивний (≈25 Вт)"}


def _hwmon(name):
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(os.path.join(d, "name")) == name:
            return d
    return None


def telemetry():
    """Температура APU (°C), оберти вентилятора, споживання GPU/APU (Вт) — що є в системі."""
    out = {"temp": None, "gpu_temp": None, "fan": None, "watts": None}
    k10 = _hwmon("k10temp")
    if k10:
        v = _read(os.path.join(k10, "temp1_input"))
        out["temp"] = round(int(v) / 1000) if v.isdigit() else None
    gpu = _hwmon("amdgpu")
    if gpu:
        v = _read(os.path.join(gpu, "temp1_input"))
        out["gpu_temp"] = round(int(v) / 1000) if v.isdigit() else None
        for f in ("power1_average", "power1_input"):
            v = _read(os.path.join(gpu, f))
            if v.isdigit():
                out["watts"] = round(int(v) / 1e6, 1)
                break
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        v = _read(os.path.join(d, "fan1_input"))
        if v.isdigit() and int(v) > 0:
            out["fan"] = int(v)
            break
    if out["temp"] is None:
        out["temp"] = out["gpu_temp"]
    return out


class PowerCtl:
    """Сходинки від найтихішої (0) до найпотужнішої. Два способи:
       profiles — профілі виробника (+ турбобуст CPU), для пристроїв, де ручний TDP гучний;
       watts — ліміт TDP у ватах (Steam Deck)."""

    def __init__(self, device, log=print):
        self.log = log
        self.tdp = Tdp(log)
        self.kind = None
        self.levels = []
        self.error = self.tdp.error
        self.boost_ok = False
        if self.tdp.cli and device.get("quiet_profiles"):
            profs = self._profiles()
            if profs:
                self.boost_ok = self.tdp._run("get-cpu-boost-state") is not None
                # тихий профіль — без турбо; далі для кожного профілю: без турбо, з турбо
                for p in profs:
                    if not self.boost_ok:
                        self.levels.append({"profile": p, "boost": None})
                        continue
                    self.levels.append({"profile": p, "boost": False})
                    if p != profs[0]:
                        self.levels.append({"profile": p, "boost": True})
                self.kind = "profiles"
        if device.get("quiet_profiles") and not self.kind:
            # тут ручний TDP = гучний «custom» — краще не керувати зовсім
            self.error = self.error or "профілі живлення не знайдено в steamos-manager"
        elif not self.kind and self.tdp.available:
            ws = list(range(self.tdp.min, self.tdp.max + 1, 2))
            if ws[-1] != self.tdp.max:
                ws.append(self.tdp.max)
            self.levels = [{"watts": w} for w in ws]
            self.kind = "watts"
        self.available = bool(self.kind)

    def _profiles(self):
        txt = self.tdp._run("get-all-properties") or ""
        found = []
        for line in txt.splitlines():
            if "vailable" in line and "rofile" in line and "erformance" in line:
                found = [w.strip(" '\"[],") for w in line.split(":", 1)[-1].replace(",", " ").split()]
        cur = (self.tdp._run("get-performance-profile") or "").split()[-1:] or [""]
        if not found and cur[0]:
            found = [p for p in PROFILE_ORDER if p in ("low-power", "balanced", "performance")]
        found = [p for p in found if p and p not in LOUD_PROFILES]
        order = {p: i for i, p in enumerate(PROFILE_ORDER)}
        return sorted({p for p in found if p in order}, key=lambda p: order[p])

    def key(self, i):
        lv = self.levels[i]
        if "watts" in lv:
            return f"w:{lv['watts']}"
        return f"p:{lv['profile']}:{'b' if lv['boost'] else 'n'}"

    def index_of(self, key):
        for i in range(len(self.levels)):
            if self.key(i) == key:
                return i
        if isinstance(key, (int, float)) and self.kind == "watts":   # старі збереження (Вт)
            return min(range(len(self.levels)), key=lambda i: abs(self.levels[i]["watts"] - key))
        return None

    def label(self, i):
        if i is None or not self.levels:
            return "—"
        lv = self.levels[i]
        if "watts" in lv:
            return f"TDP {lv['watts']} Вт"
        s = PROFILE_LABEL.get(lv["profile"], lv["profile"])
        if lv.get("boost") is not None:
            s += ", турбо CPU " + ("так" if lv["boost"] else "ні")
        return s

    def snapshot(self):
        if self.kind == "watts":
            return {"watts": self.tdp.get()}
        prof = (self.tdp._run("get-performance-profile") or "").split()
        boost = (self.tdp._run("get-cpu-boost-state") or "").split() if self.boost_ok else []
        return {"profile": prof[-1] if prof else None, "boost": boost[-1] if boost else None}

    def restore(self, snap):
        if not snap:
            return
        if snap.get("watts"):
            self.tdp.set(snap["watts"])
        if snap.get("profile"):
            self.tdp._run("set-performance-profile", snap["profile"])
        if snap.get("boost"):
            self.tdp._run("set-cpu-boost-state", snap["boost"])

    def apply(self, i):
        lv = self.levels[i]
        if "watts" in lv:
            return self.tdp.set(lv["watts"])
        ok = self.tdp._run("set-performance-profile", lv["profile"]) is not None
        if lv.get("boost") is not None:
            self.tdp._run("set-cpu-boost-state", "enabled" if lv["boost"] else "disabled")
        if not ok:
            self.log(f"живлення: профіль {lv['profile']} не застосовано: {self.tdp.error}")
        return ok

    def current_profile(self):
        out = (self.tdp._run("get-performance-profile") or "").split()
        return out[-1] if out else None
