#!/usr/bin/env python3
"""
AutoFPS autotune — автоматичне налаштування графіки гри (як оптимізація NVIDIA),
але з самонавчанням між запусками.

Запускається обгорткою ПЕРЕД стартом гри (коли гра ще не тримає свої файли):
  python3 autotune.py <appid> <аргументи %command%...>

Цикл:
  1. Визначаємо рушій (Unreal / Unity) за файлами гри.
  2. Знаходимо файл налаштувань, запам'ятовуємо оригінал.
  3. Ставимо «сходинку» якості: 0 = максимум, далі знижуємо те,
     що дає найбільше FPS за найменшу втрату картинки.
  4. Бекенд під час гри міряє p10 FPS і записує в tune.json.
  5. Наступний запуск: не тягне ціль — сходинка нижче; великий запас — вище;
     в межах норми — готово. Зміни без ефекту → припиняємо (CPU-обмеження).
"""
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofps_common as C  # noqa: E402

# ------------------------------------------------------------------ ladders

UE_KEYS = ["sg.ResolutionQuality", "sg.ViewDistanceQuality", "sg.AntiAliasingQuality",
           "sg.ShadowQuality", "sg.GlobalIlluminationQuality", "sg.ReflectionQuality",
           "sg.PostProcessQuality", "sg.TextureQuality", "sg.EffectsQuality",
           "sg.FoliageQuality", "sg.ShadingQuality"]

# Кожна наступна сходинка = попередня + ці зміни.
# Порядок: спершу дорогі для GPU й малопомітні ефекти, роздільна здатність і
# текстури — в останню чергу (16 ГБ спільної пам'яті вистачає на High).
UE_DELTAS = [
    {"ShadowQuality": 2},
    {"PostProcessQuality": 2, "EffectsQuality": 2},
    {"GlobalIlluminationQuality": 2, "ReflectionQuality": 2, "FoliageQuality": 2},
    {"ShadowQuality": 1, "ViewDistanceQuality": 2},
    {"GlobalIlluminationQuality": 1, "ReflectionQuality": 1, "PostProcessQuality": 1},
    {"ResolutionQuality": 85, "EffectsQuality": 1, "FoliageQuality": 1},
    {"ShadowQuality": 0, "AntiAliasingQuality": 2, "ShadingQuality": 2},
    {"ResolutionQuality": 75, "GlobalIlluminationQuality": 0, "ReflectionQuality": 0,
     "PostProcessQuality": 0},
    {"ResolutionQuality": 67, "ViewDistanceQuality": 1, "FoliageQuality": 0,
     "EffectsQuality": 0},
    {"ResolutionQuality": 58, "ShadingQuality": 1, "TextureQuality": 2},
    {"ResolutionQuality": 50, "AntiAliasingQuality": 1, "TextureQuality": 1},
]


def ue_ladder():
    cur = {k.split(".", 1)[1]: 3 for k in UE_KEYS}
    cur["ResolutionQuality"] = 100
    ladder = [dict(cur)]
    for d in UE_DELTAS:
        cur.update(d)
        ladder.append(dict(cur))
    return [{f"sg.{k}": str(v) for k, v in step.items()} for step in ladder]


RES_SCALES = [100, 85, 75, 67, 58, 50]


def unity_ladder(q0):
    """Чергуємо: рівень якості Unity ↓, роздільна здатність ↓."""
    q = q0 if q0 is not None else None
    si = 0
    ladder = [{"quality": q, "scale": RES_SCALES[0]}]
    turn_quality = True
    while True:
        if turn_quality and q is not None and q > 0:
            q -= 1
        elif si < len(RES_SCALES) - 1:
            si += 1
        elif q is not None and q > 0:
            q -= 1
        else:
            break
        turn_quality = not turn_quality
        ladder.append({"quality": q, "scale": RES_SCALES[si]})
    return ladder


# ------------------------------------------------------------------ ini utils

def patch_ini(lines, changes, sectioned=True):
    """changes: {section: {key: value}}. Зберігає коментарі й порядок."""
    if not sectioned:
        changes = {None: changes}
    out, section, seen = [], None, set()

    def flush(sec):
        for k, v in changes.get(sec, {}).items():
            if (sec, k) not in seen:
                out.append(f"{k}={v}\n")
                seen.add((sec, k))

    for line in lines:
        s = line.strip()
        if sectioned and s.startswith("[") and s.endswith("]"):
            # нові ключі — перед порожніми рядками, що відділяють секції
            tail = []
            while out and not out[-1].strip():
                tail.insert(0, out.pop())
            flush(section)
            out.extend(tail)
            section = s[1:-1]
            out.append(line)
            continue
        if "=" in s and not s.startswith((";", "#")):
            key = s.split("=", 1)[0].strip()
            if key in changes.get(section, {}):
                out.append(f"{key}={changes[section][key]}\n")
                seen.add((section, key))
                continue
        out.append(line)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    flush(section)
    for sec, kv in changes.items():
        if sec is not None and any((sec, k) not in seen for k in kv):
            out.append(f"\n[{sec}]\n")
            flush(sec)
    return out


def read_ini_section(path, section):
    vals, cur = {}, None
    for line in C.read_text(path).splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1]
        elif cur == section and "=" in s and not s.startswith((";", "#")):
            k, v = s.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


def _drop_lines(path, pred):
    with open(path, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(line for line in lines if not pred(line.strip()))


def backup_once(path):
    bak = path + ".autofps.bak"
    if os.path.isfile(path) and not os.path.exists(bak):
        with open(path, "rb") as src, open(bak, "wb") as dst:
            dst.write(src.read())


# ------------------------------------------------------------------ adapters

def user_root(compat):
    return os.path.join(compat, "pfx", "drive_c", "users", "steamuser")


class UnrealAdapter:
    engine = "unreal"
    SECTION = "ScalabilityGroups"

    def __init__(self, install, compat, device):
        self.install, self.compat, self.device = install, compat, device

    @staticmethod
    def detect(install):
        pats = ["*/Binaries/Win64/*Shipping*.exe", "*/Binaries/WinGDK/*Shipping*.exe",
                "Engine/Binaries/ThirdParty"]
        return any(glob.glob(os.path.join(install, p)) for p in pats)

    def projects(self):
        names = set()
        for exe in glob.glob(os.path.join(self.install, "*/Binaries/Win*/*Shipping*.exe")):
            names.add(os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(exe)))))
            names.add(os.path.basename(exe).split("-")[0])
        names.discard("Engine")
        return names

    def locate(self):
        root = user_root(self.compat)
        pats = ["AppData/Local/*/Saved/Config/*/GameUserSettings.ini",
                "Documents/My Games/*/Saved/Config/*/GameUserSettings.ini",
                "Documents/My Games/*/*/Saved/Config/*/GameUserSettings.ini",
                "Saved Games/*/Saved/Config/*/GameUserSettings.ini"]
        cands = []
        for p in pats:
            cands += glob.glob(os.path.join(root, p))
        cands = [c for c in cands if "/Engine/" not in c]
        if not cands:
            return None
        names = self.projects()
        named = [c for c in cands if any(f"/{n}/" in c for n in names)]
        pool = named or cands
        return max(pool, key=os.path.getmtime)

    def read(self, cfg):
        vals = read_ini_section(cfg, self.SECTION)
        return {k: vals[k] for k in UE_KEYS if k in vals}

    def ladder(self, original):
        return ue_ladder()

    def write(self, cfg, values):
        backup_once(cfg)
        with open(cfg, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        new = patch_ini(lines, {self.SECTION: values})
        with open(cfg, "w", encoding="utf-8") as f:
            f.writelines(new)

    def restore(self, cfg, original):
        self.write(cfg, original)
        drop = [k for k in UE_KEYS if k not in original]
        _drop_lines(cfg, lambda s: s.split("=", 1)[0].strip() in drop)

    def describe(self, values):
        return f"роздільність {values.get('sg.ResolutionQuality', '?')}%, тіні {values.get('sg.ShadowQuality', '?')}/3"


def unity_hash(name):
    """Суфікс _h<число>, який Unity додає до ключів PlayerPrefs у реєстрі Windows."""
    h = 5381
    for ch in name:
        h = ((h * 33) ^ ord(ch)) & 0xFFFFFFFF
    return f"{name}_h{h}"


UNITY_KEYS = {"quality": "UnityGraphicsQuality",
              "width": "Screenmanager Resolution Width",
              "height": "Screenmanager Resolution Height"}


class UnityAdapter:
    engine = "unity"

    def __init__(self, install, compat, device):
        self.install, self.compat, self.device = install, compat, device
        self.company, self.product = self._app_info()
        self.windows = bool(glob.glob(os.path.join(install, "*.exe")))

    @staticmethod
    def detect(install):
        return bool(glob.glob(os.path.join(install, "UnityPlayer.*"))
                    or glob.glob(os.path.join(install, "*_Data", "app.info")))

    def _app_info(self):
        for p in glob.glob(os.path.join(self.install, "*_Data", "app.info")):
            lines = C.read_text(p).splitlines()
            if len(lines) >= 2:
                return lines[0].strip(), lines[1].strip()
        return None, None

    def locate(self):
        if not self.company:
            return None
        if self.windows:
            reg = os.path.join(self.compat, "pfx", "user.reg")
            return reg if os.path.isfile(reg) and self._section(reg) else None
        prefs = os.path.join(C.HOME, ".config", "unity3d", self.company, self.product, "prefs")
        return prefs if os.path.isfile(prefs) else None

    # --- Windows-реєстр (user.reg у префіксі Proton)
    def _header(self):
        # у user.reg зворотні слеші подвоєні: [Software\\Company\\Product] 1712345678
        return f"[software\\\\{self.company}\\\\{self.product}]".lower()

    def _section(self, reg):
        lines = C.read_text(reg).splitlines()
        hdr = self._header()
        for i, line in enumerate(lines):
            if line.lower().startswith(hdr):
                j = i + 1
                while j < len(lines) and not lines[j].startswith("["):
                    j += 1
                return i, j
        return None

    def read(self, cfg):
        out = {}
        if self.windows:
            span = self._section(cfg)
            lines = C.read_text(cfg).splitlines()
            for name, key in UNITY_KEYS.items():
                pref = f'"{unity_hash(key)}"=dword:'
                for line in lines[span[0]:span[1]]:
                    if line.startswith(pref):
                        out[name] = int(line[len(pref):].strip(), 16)
        else:
            text = C.read_text(cfg)
            for name, key in UNITY_KEYS.items():
                m = re.search(rf'<pref name="{re.escape(key)}" type="int">(-?\d+)</pref>', text)
                if m:
                    out[name] = int(m.group(1))
        return out

    def ladder(self, original):
        return unity_ladder(original.get("quality"))

    def _concrete(self, values):
        if "scale" not in values:  # оригінальні значення при відновленні
            return {k: v for k, v in values.items() if v is not None}
        w, h = self.device["res"]
        s = values["scale"] / 100.0
        out = {"width": int(round(w * s / 2)) * 2, "height": int(round(h * s / 2)) * 2}
        if values.get("quality") is not None:
            out["quality"] = values["quality"]
        return out

    def write(self, cfg, values):
        vals = self._concrete(values)
        backup_once(cfg)
        if self.windows:
            span = self._section(cfg)
            if not span:
                return
            with open(cfg, encoding="utf-8", errors="ignore") as f:
                lines = f.read().split("\n")
            body = lines[span[0] + 1:span[1]]
            for name, v in vals.items():
                pref = f'"{unity_hash(UNITY_KEYS[name])}"=dword:'
                new = f"{pref}{v & 0xFFFFFFFF:08x}"
                for i, line in enumerate(body):
                    if line.startswith(pref):
                        body[i] = new
                        break
                else:
                    pos = len(body)
                    while pos > 0 and not body[pos - 1].strip():
                        pos -= 1
                    body.insert(pos, new)
            lines[span[0] + 1:span[1]] = body
            with open(cfg, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        else:
            text = C.read_text(cfg)
            for name, v in vals.items():
                key = UNITY_KEYS[name]
                tag = f'<pref name="{key}" type="int">{v}</pref>'
                pat = rf'<pref name="{re.escape(key)}" type="int">-?\d+</pref>'
                if re.search(pat, text):
                    text = re.sub(pat, tag, text)
                else:
                    text = text.replace("</unity_prefs>", f"\t{tag}\n</unity_prefs>")
            with open(cfg, "w", encoding="utf-8") as f:
                f.write(text + "\n")

    def restore(self, cfg, original):
        self.write(cfg, original)
        drop = [UNITY_KEYS[n] for n in UNITY_KEYS if n not in original]
        if self.windows:
            prefixes = tuple(f'"{unity_hash(k)}"=' for k in drop)
            _drop_lines(cfg, lambda s: bool(prefixes) and s.startswith(prefixes))
        else:
            _drop_lines(cfg, lambda s: any(f'<pref name="{k}"' in s for k in drop))

    def describe(self, values):
        q = values.get("quality")
        return f"роздільність {values.get('scale', '?')}%" + (f", якість {q}" if q is not None else "")


ADAPTERS = [UnrealAdapter, UnityAdapter]


# ------------------------------------------------------------------ decision

def decide(entry, target, n):
    """Повертає (нова_сходинка, пояснення). Змінює entry на місці."""
    lvl = max(0, min(int(entry.get("level", 0)), n - 1))
    last = entry.pop("last", None)
    if entry.get("converged") or entry.get("ineffective"):
        return lvl, entry.get("note", "")
    if not last or last.get("level") != lvl or last.get("target") != target:
        return lvl, f"Сходинка {lvl + 1}/{n}: вимірюю"

    p10 = float(last["p10"])
    hist = entry.setdefault("history", [])
    hist.append({"level": lvl, "p10": round(p10, 1)})
    del hist[:-12]

    # Якщо сходинки відрізняються на 2+, а FPS той самий — налаштування не діють
    for h in hist[:-1]:
        if abs(h["level"] - lvl) >= 2 and abs(h["p10"] - p10) / max(h["p10"], 1) < 0.04:
            entry["ineffective"] = True
            return lvl, ("Налаштування не впливають на FPS: гра впирається в процесор "
                         "або зберігає графіку деінде")

    r = p10 / target
    failed = set(entry.get("failed", []))
    if r < 0.97:
        failed.add(lvl)
        entry["failed"] = sorted(failed)
        if lvl >= n - 1:
            entry["converged"] = True
            return lvl, f"Мінімум налаштувань: {target} FPS недосяжні, лишаю нижчий рівень FPS"
        step = 1 if r >= 0.88 else 2 if r >= 0.75 else 3
        new = min(n - 1, lvl + step)
        return new, f"p10 {p10:.0f} < {target}: знижую до сходинки {new + 1}/{n}"
    if r > 1.25 and lvl > 0 and (lvl - 1) not in failed:
        step = 2 if r > 1.6 and lvl > 1 and (lvl - 2) not in failed else 1
        new = lvl - step
        return new, f"p10 {p10:.0f} — є запас, підвищую якість до сходинки {new + 1}/{n}"
    entry["converged"] = True
    return lvl, f"Готово: сходинка {lvl + 1}/{n}, p10 {p10:.0f} при цілі {target}"


# ------------------------------------------------------------------ main

def find_install(args):
    env = os.environ.get("STEAM_COMPAT_INSTALL_PATH")
    if env and os.path.isdir(env):
        return env
    for a in args:
        m = re.search(r"(.*/steamapps/common/[^/]+)", a)
        if m and os.path.isdir(m.group(1)):
            return m.group(1)
    return None


def find_compat(appid):
    env = os.environ.get("STEAM_COMPAT_DATA_PATH")
    if env:
        return env
    return os.path.join(C.HOME, ".local/share/Steam/steamapps/compatdata", appid)


def run(appid, args, device=None, log=print):
    settings = C.load_json(C.SETTINGS_FILE, {})
    if not settings.get("autotune", True) or appid in ("", "0"):
        return
    if os.path.isfile(os.path.join(C.PROFILES_DIR, f"{appid}.json")):
        import apply_profile  # ручний профіль має пріоритет
        apply_profile.main_for(appid)
        return

    device = device or C.detect_device()
    tune = C.load_json(C.TUNE_FILE, {})
    key = f"{appid}:{device['id']}"
    entry = tune.setdefault(key, {"level": device["start_level"]})

    install = find_install(args) or entry.get("install")
    if not install:
        entry["note"] = "Не знайшов теку гри"
        C.save_json(C.TUNE_FILE, tune)
        return
    entry["install"] = install
    compat = find_compat(appid)

    adapter = None
    for A in ADAPTERS:
        if (entry.get("engine") in (None, A.engine)) and A.detect(install):
            adapter = A(install, compat, device)
            break
    if not adapter:
        entry["engine"] = "unknown"
        entry["note"] = "Рушій не підтримується — працює лише підбір FPS"
        C.save_json(C.TUNE_FILE, tune)
        return
    entry["engine"] = adapter.engine

    cfg = adapter.locate()
    if not cfg:
        entry["note"] = ("Файл налаштувань ще не створено — зіграйте раз і вийдіть "
                         "через меню гри, далі підбір почнеться сам")
        entry.pop("applied_level", None)
        C.save_json(C.TUNE_FILE, tune)
        return
    entry["config"] = cfg
    if "original" not in entry:
        entry["original"] = adapter.read(cfg)

    if entry.get("restore_pending"):
        adapter.restore(cfg, entry["original"])
        entry.update(restore_pending=False, disabled=True,
                     note="Повернуто оригінальні налаштування гри")
        entry.pop("applied_level", None)
        C.save_json(C.TUNE_FILE, tune)
        return
    if entry.get("disabled"):
        entry.pop("applied_level", None)
        C.save_json(C.TUNE_FILE, tune)
        return

    target = C.quality_target(settings, device)
    if entry.get("target") != target:
        for k in ("history", "failed", "converged", "ineffective", "last"):
            entry.pop(k, None)
        entry["target"] = target

    ladder = adapter.ladder(entry["original"])
    level, note = decide(entry, target, len(ladder))
    values = ladder[level]
    adapter.write(cfg, values)
    entry.update(level=level, applied_level=level, steps=len(ladder),
                 note=note, summary=adapter.describe(values))
    C.save_json(C.TUNE_FILE, tune)
    log(f"{appid}: {adapter.engine} сходинка {level + 1}/{len(ladder)} — {note}")


def main():
    if len(sys.argv) < 2:
        return
    try:
        run(sys.argv[1], sys.argv[2:])
    except Exception as e:  # гру запускаємо за будь-яких умов
        print(f"autotune error: {e!r}")


if __name__ == "__main__":
    main()
