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
