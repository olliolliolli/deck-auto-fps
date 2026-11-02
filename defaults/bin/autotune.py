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
           "sg.FoliageQuality", "sg.ShadingQuality", "sg.LandscapeQuality", "sg.StreamingQuality"]

# Налаштування поза [ScalabilityGroups], які багато ігор UE тримають у своєму розділі
# GameUserSettings.ini (напр. [LotDKLocalSettings]). Пишемо лише ті, що є у файлі, і не вище
# найбільшого значення, яке там колись стояло (максимум самої гри).
UE_EXTRA_FOLLOW = {"MeshQuality": "ShadingQuality", "MaterialQuality": "ShadingQuality",
                   "DistanceFieldAmbientOcclusion": "GlobalIlluminationQuality"}
UE_EXTRA_KEYS = list(UE_EXTRA_FOLLOW) + ["Anisotropy"]
UE_NOT_GRAPHICS = ("upscal", "dlss", "fsr", "xess", "tsr", "audio", "sound", "population",
                   "traffic", "crowd", "physics", "resolution", "preset", "framegen", "sharp")


def ue_follow(name):
    """За якою стандартною групою sg.* іде нестандартне налаштування (sg.HairQuality тощо)."""
    n = name.lower()
    table = [(("volumetric", "fog", "cloud", "sky", "atmospher", "lumen", "gi", "illum"), "GlobalIlluminationQuality"),
             (("reflect", "ssr"), "ReflectionQuality"),
             (("shadow",), "ShadowQuality"),
             (("motionblur", "dof", "depthoffield", "bloom", "postprocess", "lens"), "PostProcessQuality"),
             (("hair", "fx", "effect", "particle", "water", "destruct"), "EffectsQuality"),
             (("foliage", "grass", "vegetation", "tree"), "FoliageQuality"),
             (("view", "distance", "lod", "landscape", "terrain"), "ViewDistanceQuality"),
             (("texture", "streaming"), "TextureQuality"),
             (("mesh", "material", "object", "detail", "environment", "shading", "shader", "geometry"), "ShadingQuality")]
    for words, grp in table:
        if any(w in n for w in words):
            return grp
    return "EffectsQuality"


def ue_is_quality(name, value):
    n = name.lower()
    if not n.endswith("quality") or any(w in n for w in UE_NOT_GRAPHICS):
        return False
    return bool(re.fullmatch(r"\s*[0-4]\s*", str(value)))

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


def ue_ladder(lowres=None):
    """lowres=67 — та сама сходинка якості, але роздільність рендеру зафіксовано (≈HD)."""
    cur = {k.split(".", 1)[1]: 3 for k in UE_KEYS}
    cur["ResolutionQuality"] = lowres or 100
    ladder = [dict(cur)]
    for d in UE_DELTAS:
        cur.update({k: v for k, v in d.items() if not (lowres and k == "ResolutionQuality")})
        cur["LandscapeQuality"] = cur["ViewDistanceQuality"]
        ladder.append(dict(cur))
    return [{f"sg.{k}": str(v) for k, v in step.items()} for step in ladder]


# ---- Роздільність заради графіки: для екранів 1080p+ (Legion Go S 1920×1200) можна рендерити
# у ≈HD (1280×800) і натомість тримати вищі налаштування. Вирішує замір, а не здогадка.
LOWRES_HEIGHT = 800
LOWRES_MIN_GAIN = 3      # мінімум на скільки сходинок якості має стати краще, щоб варто було


def lowres_scale(device):
    """Масштаб (%) рендеру ≈HD для цього екрана або None, якщо екран і так ≤ 1080p."""
    h = int((device or {}).get("res", [0, 0])[1])
    if h < 1080:
        return None
    return int(round(LOWRES_HEIGHT * 100.0 / h))


RES_SCALES = [100, 85, 75, 67, 58, 50]


def unity_ladder(q0, lowres=None):
    """Чергуємо: рівень якості Unity ↓, роздільна здатність ↓.
    lowres — роздільність зафіксовано на ≈HD, знижується лише рівень якості."""
    if lowres:
        if q0 is None:
            return [{"quality": None, "scale": lowres}]
        return [{"quality": q, "scale": lowres} for q in range(q0, -1, -1)]
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


def changed_by_user(applied, current):
    """Чи змінено значення, які ми записали минулого разу (у меню гри)."""
    for k, v in applied.items():
        if k in current:
            try:
                if abs(float(current[k]) - float(v)) > 0.01:
                    return True
            except (TypeError, ValueError):
                if str(current[k]) != str(v):
                    return True
    return False


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
        self.cfg = max(pool, key=os.path.getmtime)
        return self.cfg

    def _extra_where(self, cfg):
        """{ключ: розділ} для додаткових налаштувань якості, знайдених у файлі."""
        where, cur = {}, None
        for line in C.read_text(cfg).splitlines():
            st = line.strip()
            if st.startswith("[") and st.endswith("]"):
                cur = st[1:-1]
            elif cur and cur != self.SECTION and "=" in st and not st.startswith((";", "#")):
                k, v = (x.strip() for x in st.split("=", 1))
                if k not in where and (k in UE_EXTRA_KEYS or ue_is_quality(k, v)) \
                        and not cur.startswith("/Script/Engine."):
                    where[k] = cur
        return where

    def read(self, cfg):
        vals = read_ini_section(cfg, self.SECTION)
        out = {k: vals[k] for k in UE_KEYS if k in vals}
        for k, v in vals.items():            # нестандартні групи гри (sg.HairQuality, …)
            if k.startswith("sg.") and k not in out and ue_is_quality(k[3:], v):
                out[k] = v
        for k, sec in self._extra_where(cfg).items():
            v = read_ini_section(cfg, sec).get(k)
            if v is not None:
                out[f"x.{k}"] = v
        return out

    def ladder(self, original, lowres=None):
        steps = ue_ladder(lowres)
        handheld = (self.device or {}).get("id") in HANDHELDS
        extra_max = getattr(self, "extra_max", {}) or {}
        cur = self.read(self.cfg) if getattr(self, "cfg", None) else {}
        extras = [k for k in cur if k.startswith("x.") or (k.startswith("sg.") and k not in UE_KEYS)]
        n = len(steps)
        for i, st in enumerate(steps):
            if handheld:
                st["sg.TextureQuality"] = str(min(int(st["sg.TextureQuality"]), UE_HANDHELD_TEXTURE_MAX))
            st["sg.StreamingQuality"] = st["sg.TextureQuality"]
            for k in extras:
                name = k.split(".", 1)[1]
                cap = int(extra_max.get(k, extra_max.get(name, 3)))
                if name == "Anisotropy":
                    want = 16 if i <= n * 0.3 else 8 if i <= n * 0.65 else 4
                else:
                    want = int(st[f"sg.{UE_EXTRA_FOLLOW.get(name) or ue_follow(name)}"])
                st[k] = str(int(min(want, cap)))
        return steps

    def rank(self, values, ladder):
        """Наскільки знижено налаштування (без роздільності): номер сходинки."""
        return ladder.index(values) if values in ladder else 0

    def res_scale(self, values):
        try:
            return int(float(values.get("sg.ResolutionQuality", 100)))
        except ValueError:
            return 100

    def write(self, cfg, values):
        backup_once(cfg)
        with open(cfg, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        changes = {self.SECTION: {k: v for k, v in values.items() if not k.startswith("x.")}}
        where = self._extra_where(cfg)
        for k, v in values.items():
            if k.startswith("x.") and k[2:] in where:
                changes.setdefault(where[k[2:]], {})[k[2:]] = v
        new = patch_ini(lines, changes)
        with open(cfg, "w", encoding="utf-8") as f:
            f.writelines(new)

    def restore(self, cfg, original):
        self.write(cfg, original)
        drop = [k for k in UE_KEYS if k not in original]
        _drop_lines(cfg, lambda s: s.split("=", 1)[0].strip() in drop)

    def comparable(self, values):
        return dict(values)

    def describe(self, values):
        lvl = ["низькі", "середні", "високі", "ультра"]

        def q(key):
            try:
                return lvl[max(0, min(3, int(float(values[f"sg.{key}"]))))]
            except (KeyError, ValueError):
                return "?"
        return (f"роздільність {values.get('sg.ResolutionQuality', '?')}%, тіні {q('ShadowQuality')}, "
                f"освітлення {q('GlobalIlluminationQuality')}, ефекти {q('PostProcessQuality')}, "
                f"шейдинг {q('ShadingQuality')}, текстури {q('TextureQuality')}")


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
        # читаємо без strip(): індекси рядків мають збігатися з write()
        with open(reg, encoding="utf-8", errors="ignore") as f:
            lines = f.read().split("\n")
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
            if not span:
                return out
            with open(cfg, encoding="utf-8", errors="ignore") as f:
                lines = f.read().split("\n")
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

    lowres_min_gain = 2        # рівнів якості Unity мало (зазвичай 0–5), кожен — помітний

    def ladder(self, original, lowres=None):
        return unity_ladder(original.get("quality"), lowres)

    def rank(self, values, ladder):
        q0 = ladder[0].get("quality")
        q = values.get("quality")
        return 0 if q0 is None or q is None else q0 - q

    def res_scale(self, values):
        return int(values.get("scale", 100))

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

    def comparable(self, values):
        # роздільну здатність не порівнюємо: гра може зберегти найближчий режим екрана
        q = values.get("quality")
        return {} if q is None else {"quality": q}

    def describe(self, values):
        c = self._concrete(values)
        q = values.get("quality")
        res = f"роздільність {values.get('scale', '?')}% ({c['width']}×{c['height']})"
        return res + (f", рівень якості {q}" if q is not None else "")


# Capcom RE Engine: local_config.ini у теці гри, секція [AppRender], цілі числа (0 = найнижче).
# Максимуми не задокументовані, тож стеля — те, що записала сама гра (твій конфіг або
# local_config_default.ini); вище плагін не ставить ніколи.
RE_KEYS = [  # порядок: що дає найбільше FPS за найменшу втрату картинки
    "AppRayTraceOnOff", "AppStrandHair", "AppShadowQuality", "AppVolumeLightQuality",
    "AppAmbientOcclusion", "AppSSRefrection", "AppContactShadow", "AppSSSSSetting",
    "AppParticleLighting", "AppMeshQuality", "AppTessellation", "AppLensFrare", "AppBloom",
]
# ---- Портативна політика: те, чого не видно на 7–8" екрані, але що з'їдає GPU/відеопам'ять,
# вимикаємо одразу (як «оптимальні» пресети NVIDIA), а не чекаємо, поки FPS впаде.
HANDHELDS = {"deck_lcd", "deck_oled", "legion_go_s"}
# RE Engine: трасування, волосся-пасма, SSR, контактні тіні — непомітні на малому екрані
RE_HANDHELD_OFF = ["AppRayTraceOnOff", "AppStrandHair", "AppSSRefrection", "AppContactShadow"]
# Текстури — за бюджетом відеопам'яті (індекс у меню гри; RE4: 0 — Low 0.25 ГБ … 10 — High 8 ГБ).
# Спільна пам'ять портативки ділиться з системою: великі текстури не видно на екрані,
# зате вони переповнюють пам'ять — звідси підвисання. Legion Go S: ~1–2 ГБ, Steam Deck: ~0.5 ГБ.
RE_TEXTURE_KEY = "AppTextureQuality"
RE_TEXTURE_BUDGET = {"legion_go_s": 4, "deck_lcd": 2, "deck_oled": 2}
# Unreal: текстури не вище «High» (3 — Epic/Cinematic вже не помітні на портативці)
UE_HANDHELD_TEXTURE_MAX = 2

RE_FSR2_KEY = "AppUpScalingTypeFSR2"   # 1 «Якість», 2 «Баланс», 3 «Продуктивність» (з мостом — FSR 4)
RE_TO_ZERO = {"AppRayTraceOnOff", "AppStrandHair", "AppSSRefrection", "AppContactShadow"}
RE_LABELS = {"AppShadowQuality": "тіні", "AppVolumeLightQuality": "об'ємне світло",
             "AppAmbientOcclusion": "AO", "AppMeshQuality": "моделі", "AppStrandHair": "волосся",
             "AppRayTraceOnOff": "трасування"}


class REEngineAdapter:
    engine = "re_engine"
    SECTION = "AppRender"

    def __init__(self, install, compat, device):
        self.install, self.compat, self.device = install, compat, device
        self.appid = ""

    @staticmethod
    def detect(install):
        return bool(glob.glob(os.path.join(install, "re_chunk_000.pak*")))

    def fsr4_bridge(self):
        """У грі стоїть наш міст FSR2→FSR4 — тоді першим знижуємо роздільність (режим FSR 2)."""
        g = C.load_json(os.path.join(C.CFG_DIR, "optiscaler_games", f"{self.appid}.json"), {})
        return bool(g.get("enabled") and g.get("method") == "bridge")

    def _inis(self, default):
        out = []
        for p in sorted(glob.glob(os.path.join(self.install, "*.ini"))):
            name = os.path.basename(p).lower()
            if ("default" in name) != default:
                continue
            if "[AppRender]" in C.read_text(p):
                out.append(p)
        return out

    def locate(self):
        cands = self._inis(default=False)
        pref = [p for p in cands if os.path.basename(p).lower() == "local_config.ini"]
        return (pref or cands or [None])[0]

    def read(self, cfg):
        vals = read_ini_section(cfg, self.SECTION)
        return {k: vals[k] for k in RE_KEYS if k in vals and vals[k].lstrip("-").isdigit()}

    def _ceiling(self, original):
        ceil = {k: int(v) for k, v in original.items()}
        for d in self._inis(default=True):
            for k, v in self.read(d).items():
                if k in ceil:
                    ceil[k] = max(ceil[k], int(v))
        dev = (self.device or {}).get("id")
        if dev in HANDHELDS:
            for k in RE_HANDHELD_OFF:
                if k in ceil:
                    ceil[k] = 0
            budget = RE_TEXTURE_BUDGET.get(dev)
            cur_tex = self._texture()
            if budget is not None and cur_tex is not None:
                ceil[RE_TEXTURE_KEY] = min(cur_tex, budget)
        return ceil

    def _texture(self):
        cfg = self.locate()
        vals = read_ini_section(cfg, self.SECTION) if cfg else {}
        v = vals.get(RE_TEXTURE_KEY, "")
        return int(v) if v.lstrip("-").isdigit() else None

    lowres_supported = False   # роздільність RE Engine не з'ясована — не чіпаємо
    ceiling_from_user = True   # вище за те, що ставив користувач (чи гра за замовч.), не йдемо

    def rank(self, values, ladder):
        return ladder.index(values) if values in ladder else 0

    def res_scale(self, values):
        return 100

    def ladder(self, original, lowres=None):
        cur = self._ceiling(original)
        if self.fsr4_bridge():
            # З FSR 4 роздільність — найдешевший важіль: спершу «Якість» → «Баланс» →
            # «Продуктивність» за повних налаштувань, і лише потім — графіка (на «Продуктивності»).
            # режими FSR, що не опускають рендер нижче ~520p (інакше навіть FSR 4 «мило»)
            out_h = int((self.device or {}).get("res", [0, 1200])[1])
            modes = [m for m, ratio in ((1, 1.5), (2, 1.7), (3, 2.0)) if out_h / ratio >= 520] or [1]
            res_steps = [dict(cur, **{RE_FSR2_KEY: m}) for m in modes]
            cur = dict(cur, **{RE_FSR2_KEY: modes[-1]})
            rest = self._settings_steps(cur)
            return [{k: str(v) for k, v in st.items()} for st in res_steps + rest[1:]]
        return [{k: str(v) for k, v in st.items()} for st in self._settings_steps(cur)]

    def _settings_steps(self, cur):
        cur = dict(cur)
        ladder = [dict(cur)]
        keys = [k for k in RE_KEYS if k in cur and k != RE_TEXTURE_KEY]
        for _ in range(14):
            changed = 0
            for k in keys:
                if cur[k] > 0:
                    cur[k] = 0 if k in RE_TO_ZERO else cur[k] - 1
                    changed += 1
                    if changed == 2:     # не більше двох змін на сходинку
                        break
            if not changed:
                break
            ladder.append(dict(cur))
        return ladder

    def write(self, cfg, values):
        backup_once(cfg)
        with open(cfg, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        new = patch_ini(lines, {self.SECTION: {k: v for k, v in values.items()
                                               if k in RE_KEYS or k in (RE_FSR2_KEY, RE_TEXTURE_KEY)}})
        with open(cfg, "w", encoding="utf-8") as f:
            f.writelines(new)

    def restore(self, cfg, original):
        self.write(cfg, original)

    def comparable(self, values):
        return dict(values)

    def describe(self, values):
        parts = []
        if RE_FSR2_KEY in values:
            parts.append("FSR 4 «" + {"1": "Якість", "2": "Баланс", "3": "Продуктивність", "4": "Ультра"}.get(
                str(values[RE_FSR2_KEY]), "?") + "»")
        parts += [f"{RE_LABELS[k]} {values[k]}" for k in RE_LABELS if k in values]
        if RE_TEXTURE_KEY in values:
            parts.append(f"текстури {values[RE_TEXTURE_KEY]}")
        return ", ".join(parts) or "налаштування RE Engine"


ADAPTERS = [UnrealAdapter, UnityAdapter, REEngineAdapter]


# ------------------------------------------------------------------ decision

STEP_GAIN = 0.09   # одна сходинка драбини ≈ +9% FPS, поки замірів гри мало
MARGIN = 1.04      # ціль + 4% запасу: важчі сцени не мають провалювати ліміт


def _gain(hist):
    """Приріст FPS на сходинку з замірів цієї гри (лог-лінійна регресія), інакше типовий."""
    import math
    pts = {}
    for h in hist:
        pts.setdefault(int(h["level"]), []).append(float(h["p10"]))
    if len(pts) < 2:
        return 1 + STEP_GAIN
    xs = sorted(pts)
    ys = [math.log(max(1.0, sum(pts[x]) / len(pts[x]))) for x in xs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else math.log(1 + STEP_GAIN)
    return min(1.25, max(1.03, math.exp(b)))


def predict_level(hist, target, n, fallback):
    """Сходинка, на якій гра (за замірами) тягне target·MARGIN; без замірів — fallback."""
    import math
    if not hist:
        return fallback
    g = _gain(hist)
    best = None
    for lv in range(n):
        # прогноз від найближчого заміру
        h = min(hist, key=lambda h: abs(int(h["level"]) - lv))
        p = float(h["p10"]) * g ** (lv - int(h["level"]))
        if p >= target * MARGIN:
            best = lv
            break
    return n - 1 if best is None else best


def first_guess(target, device, n):
    """Перший запуск без замірів: типова сходинка для цілі на цьому пристрої."""
    frac = 0.15 if target <= 30 else 0.35 if target <= 40 else 0.5 if target <= 48 else \
        0.7 if target <= 60 else 0.95
    if (device or {}).get("id") in ("deck_lcd", "deck_oled"):
        frac += 0.15
    return max(0, min(n - 1, int(round(frac * (n - 1)))))


def decide(entry, target, n):
    """Повертає (нова_сходинка, пояснення). Змінює entry на місці.

    Усі заміри гри (на будь-якій сходинці й за будь-якої цілі) — це модель «FPS від сходинки».
    Новий замір уточнює модель, і одразу стрибаємо на сходинку, що за прогнозом тягне ціль.
    Межі: failed — сходинки, що ціль не тягнули, ok — що тягнули (для поточної цілі)."""
    lvl = max(0, min(int(entry.get("level", 0)), n - 1))
    last = entry.pop("last", None)
    if entry.get("converged") or entry.get("ineffective"):
        return lvl, entry.get("decision_note") or entry.get("note", "")
    hist = entry.setdefault("history", [])
    if last and last.get("level") == lvl:
        hist.append({"level": lvl, "p10": round(float(last["p10"]), 1)})
        del hist[:-16]
    elif not hist or all(int(h["level"]) != lvl for h in hist):
        return lvl, f"Сходинка {lvl + 1}/{n}: вимірюю"
    here = [float(h["p10"]) for h in hist if int(h["level"]) == lvl]
    p10 = sum(here[-2:]) / len(here[-2:])

    # Сходинки відрізняються на 2+, а FPS той самий — налаштування не діють
    for h in hist:
        if abs(int(h["level"]) - lvl) >= 2 and abs(float(h["p10"]) - p10) / max(float(h["p10"]), 1) < 0.04:
            entry["ineffective"] = True
            return lvl, ("Налаштування не впливають на FPS: гра впирається в процесор "
                         "або зберігає графіку деінде")

    failed = set(entry.get("failed", []))
    ok = set(entry.get("ok", []))
    r = p10 / target
    (ok if r >= 0.97 else failed).add(lvl)
    entry["failed"], entry["ok"] = sorted(failed), sorted(ok)

    want = predict_level(hist, target, n, lvl)
    lo = max(failed) + 1 if failed else 0           # вище за якістю — вже не тягнули
    hi = min(ok) if ok else n - 1                   # нижче за якістю — вже тягнули
    want = max(lo, min(hi, want))
    if lvl in ok and 0.97 <= r <= 1.2 and want < lvl:
        want = lvl                                  # малий запас — не ганяємось
    if want == lvl and lvl in failed:
        want = min(n - 1, lvl + 1)
    if want == lvl:
        entry["converged"] = True
        if lvl in failed:
            return lvl, (f"Навіть на мінімумі ≈{p10:.0f} FPS — ціль {target} недосяжна. "
                         "Знизь ліміт FPS або ввімкни генерацію кадрів")
        return lvl, f"Готово: сходинка {lvl + 1}/{n}, ≈{p10:.0f} FPS при цілі {target}"
    if want in ok and want == lo:
        entry["converged"] = True
        return want, f"Готово: сходинка {want + 1}/{n} — вища не тягне {target} FPS"
    verb = "підвищую якість" if want < lvl else "знижую"
    return want, f"≈{p10:.0f} FPS на сходинці {lvl + 1} при цілі {target}: {verb} до сходинки {want + 1}/{n}"


def nearest_level(values, ladder):
    """Сходинка драбини, найближча до поточних налаштувань гри (None — не порівняти)."""
    best, best_d = None, None
    for i, st in enumerate(ladder):
        common = [k for k in st if k in values]
        if not common:
            continue
        d = 0.0
        for k in common:
            try:
                diff = abs(float(st[k]) - float(values[k]))
            except (TypeError, ValueError):
                diff = 0.0 if str(st[k]) == str(values[k]) else 1.0
            d += diff / 15.0 if "Resolution" in k or k == "scale" else diff
        if best_d is None or d < best_d - 1e-9:
            best, best_d = i, d
    return best


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
    # роздільність гри, вибрана в AutoFPS (Steam «Game resolution»): графіка підбирається під неї
    res = C.load_json(os.path.join(C.CFG_DIR, "resolution.json"), {}).get(str(appid))
    if res and re.match(r"^\d+x\d+$", res):
        w, h = (int(x) for x in res.split("x"))
        device = dict(device, res=[w, h], res_override=True)
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
        if entry.get("engine") in (None, "unknown", A.engine) and A.detect(install):
            adapter = A(install, compat, device)
            adapter.appid = appid
            break
    if not adapter:
        try:
            import generic_settings as G
            adapter = G.GenericAdapter.find(install, compat, device)
            if adapter:
                adapter.appid = appid
        except Exception as e:  # noqa: BLE001
            log(f"generic settings: {e!r}")
            adapter = None
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
    if hasattr(adapter, "_extra_where"):
        # максимум кожного додаткового налаштування, що будь-коли стояв у файлі (стеля гри)
        xm = entry.setdefault("extra_max", {})
        for k, v in adapter.read(cfg).items():
            if k.startswith("x.") or (k.startswith("sg.") and k not in UE_KEYS):
                try:
                    xm[k] = max(int(float(v)), int(xm.get(k, xm.get(k.split(".", 1)[1], 0))))
                except ValueError:
                    pass
        adapter.extra_max = xm
    elif hasattr(adapter, "update_max"):
        adapter.update_max(entry.setdefault("extra_max", {}))
    if "original" not in entry:
        entry["original"] = adapter.read(cfg)

    if entry.get("restore_pending"):
        adapter.restore(cfg, entry["original"])
        entry.update(restore_pending=False, disabled=True,
                     note="Повернуто оригінальні налаштування гри")
        entry.pop("applied_values", None)
        entry.pop("applied_level", None)
        C.save_json(C.TUNE_FILE, tune)
        return
    if entry.get("disabled"):
        entry.pop("applied_level", None)
        C.save_json(C.TUNE_FILE, tune)
        return

    # Користувач змінив графіку в меню гри.
    # За замовчуванням підбір триває (плагін повертає підібрані налаштування);
    # з увімкненим «Залишати мої ручні налаштування» — більше не чіпаємо гру.
    respect = bool(settings.get("respect_manual_graphics", False))
    manual_note = ""
    applied = entry.get("applied_values")
    if applied and changed_by_user(applied, adapter.read(cfg)):
        if respect:
            entry["user_override"] = True
        elif getattr(adapter, "ceiling_from_user", False):
            # RE Engine: максимуми невідомі, стеля — те, що ставив користувач. Підвищив у меню →
            # це нова стеля; підбір починається заново від неї (а не повертає стару низьку).
            raised = False
            for k, v in adapter.read(cfg).items():
                try:
                    if int(v) > int(entry["original"].get(k, -1)):
                        entry["original"][k] = v
                        raised = True
                except ValueError:
                    pass
            if raised:
                for k in ("history", "failed", "ok", "converged", "ineffective", "last",
                          "start_scaled", "track", "native_result", "lowres_done"):
                    entry.pop(k, None)
                manual_note = "Ти підвищив графіку в меню — це нова стеля, підбираю від неї. "
            else:
                manual_note = "Графіку змінено в меню гри — повертаю підібрану. "
        else:
            manual_note = "Графіку змінено в меню гри — повертаю підібрану. "
    if not respect:
        entry.pop("user_override", None)
    if entry.get("user_override"):
        entry.pop("applied_level", None)
        entry["note"] = "Графіку змінено в меню гри — залишаю твої налаштування"
        entry["summary"] = ""
        C.save_json(C.TUNE_FILE, tune)
        log(f"{appid}: ручні налаштування користувача, не чіпаю")
        return

    target = C.quality_target(settings, device)
    allow_low = bool(settings.get("lowres_for_quality", True))
    if not allow_low and entry.get("track") == "lowres":
        for k in ("history", "failed", "ok", "converged", "ineffective", "last", "track",
                  "native_result", "lowres_done"):
            entry.pop(k, None)
    if entry.get("target") != target:
        # заміри FPS від цілі не залежать — лишаємо; межі «тягне / не тягне» — для нової цілі
        for k in ("failed", "ok", "converged", "native_result", "lowres_done"):
            entry.pop(k, None)
        if entry.get("history"):
            entry["level"] = predict_level(entry["history"], target, int(entry.get("steps") or 12),
                                           int(entry.get("level", 0)))
        entry["target"] = target

    scale = lowres_scale(device) if getattr(adapter, "lowres_supported", True) \
        and not device.get("res_override") else None
    track = entry.get("track", "native")
    ladder = adapter.ladder(entry["original"], scale if track == "lowres" else None)
    # Стартова сходинка задана для 12-сходинкової драбини Unreal. У коротших (RE Engine — 4–8)
    # вона означала б «усе на мінімум» — масштабуємо пропорційно (один раз, до першого заміру).
    # Драбина змінилась (напр. увімкнено міст FSR 4 — з'явились кроки роздільності): підбір заново
    sig = f"{len(ladder)}:{RE_FSR2_KEY in ladder[0]}:hh2:{device.get('res')}"
    if entry.get("ladder_sig") != sig and (entry.get("ladder_sig") or RE_FSR2_KEY in ladder[0]):
        for k in ("history", "failed", "ok", "converged", "ineffective", "last", "start_scaled"):
            entry.pop(k, None)
    entry["ladder_sig"] = sig
    if RE_FSR2_KEY in ladder[0] and not entry.get("start_scaled") and not entry.get("history"):
        # старт: найнижчий дозволений режим FSR за повних (портативних) налаштувань
        modes_n = sum(1 for st in ladder if all(st.get(k) == ladder[0].get(k) for k in ladder[0] if k != RE_FSR2_KEY))
        entry["level"] = max(0, modes_n - 1)
        entry.pop("last", None)
        entry["start_scaled"] = True
    if not entry.get("start_scaled") and not entry.get("history"):
        # старт — від того, що стоїть у меню гри (напр. ти виставив максимум і натиснув
        # «Підібрати заново»); не вдалося порівняти — типова стартова сходинка пристрою
        entry["level"] = first_guess(target, device, len(ladder))
        entry.pop("last", None)
        entry.pop("start_from_user", None)
    entry["start_scaled"] = True
    level, note = decide(entry, target, len(ladder))

    # Підбір на повній роздільності завершено → чи не краще ≈HD з вищою графікою?
    if track == "native" and entry.get("converged") and not entry.get("ineffective") \
            and allow_low and scale and not entry.get("lowres_done"):
        nat = ladder[level]
        r_nat = adapter.rank(nat, ladder)
        min_gain = getattr(adapter, "lowres_min_gain", LOWRES_MIN_GAIN)
        if r_nat >= min_gain and adapter.res_scale(nat) >= scale + 15:
            entry["native_result"] = {"level": level, "rank": r_nat,
                                      "res": adapter.res_scale(nat), "note": note}
            for k in ("history", "failed", "ok", "converged", "last"):
                entry.pop(k, None)
            track = entry["track"] = "lowres"
            ladder = adapter.ladder(entry["original"], scale)
            # ті самі налаштування на ≈HD гарантовано тягнуть ціль — далі підбір піде вгору
            level = min(len(ladder) - 1, r_nat)
            note = (f"На повній роздільності вийшло лише {r_nat + 1}-ю сходинкою — пробую рендер "
                    f"{scale}% (≈{LOWRES_HEIGHT}p) з вищою графікою")
        else:
            entry["lowres_done"] = True

    # Підбір на ≈HD завершено → порівняти з повною роздільністю й вибрати краще
    elif track == "lowres" and entry.get("converged") and not entry.get("lowres_done"):
        nat = entry.get("native_result") or {}
        gain = int(nat.get("rank", 0)) - adapter.rank(ladder[level], ladder)
        entry["lowres_done"] = True
        if gain >= getattr(adapter, "lowres_min_gain", LOWRES_MIN_GAIN) and not entry.get("ineffective"):
            note = (f"Рендер {scale}% (≈{LOWRES_HEIGHT}p): графіка на {gain} сходин. краща, ніж на "
                    f"повній роздільності при тих самих {target} FPS — лишаю так")
        else:
            entry.pop("track", None)
            track = "native"
            ladder = adapter.ladder(entry["original"], None)
            level = max(0, min(int(nat.get("level", level)), len(ladder) - 1))
            entry["converged"] = True
            entry.pop("ineffective", None)
            note = (f"≈{LOWRES_HEIGHT}p дає замало виграшу ({max(gain, 0)} сходин.) — лишаю повну "
                    "роздільність. " + (nat.get("note") or ""))
        entry["decision_note"] = note

    values = ladder[level]
    adapter.write(cfg, values)
    prev = entry.get("applied_values")
    entry.update(level=level, applied_level=level, steps=len(ladder), lowres=(track == "lowres"),
                 note=manual_note + note, decision_note=note,
                 summary=adapter.describe(values),
                 applied_values=adapter.comparable(values),
                 changed=prev != adapter.comparable(values))
    C.save_json(C.TUNE_FILE, tune)
    log(f"{appid}: {adapter.engine} сходинка {level + 1}/{len(ladder)} — {note}")


def tuning_now(appid, device=None):
    """Чи цей запуск — крок підбору графіки (тоді обгортка міряє без генерації кадрів)."""
    device = device or C.detect_device()
    e = C.load_json(C.TUNE_FILE, {}).get(f"{appid}:{device['id']}") or {}
    return bool(e.get("applied_level") is not None and not e.get("converged")
                and not e.get("ineffective") and not e.get("disabled")
                and not e.get("user_override"))


def main():
    if len(sys.argv) < 2:
        return
    try:
        run(sys.argv[1], sys.argv[2:])
    except Exception as e:  # гру запускаємо за будь-яких умов
        print(f"autotune error: {e!r}")
    try:
        os.makedirs(C.CACHE_DIR, exist_ok=True)
        with open(os.path.join(C.CACHE_DIR, "tuning_now"), "w") as f:
            f.write("1" if tuning_now(sys.argv[1]) else "0")
    except Exception as e:  # noqa: BLE001
        print(f"tuning flag error: {e!r}")


if __name__ == "__main__":
    main()
