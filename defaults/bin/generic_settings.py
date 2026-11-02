"""Універсальний підбір графіки для ігор не на Unreal / Unity / RE Engine.

Знаходить файл налаштувань гри в префіксі Proton, розбирає його (кілька форматів),
сам визначає, які налаштування — це якість графіки і яка в них шкала значень,
і будує драбину з 12 сходинок: спершу знижуються дорогі для GPU й малопомітні ефекти,
потім деталізація й дальність, текстури — в останню чергу.

Формати:
  fromsoft — GraphicsConfig.xml (UTF-16): <ShadowQuality>MAX</ShadowQuality>
             (Elden Ring, Sekiro, Dark Souls III, Armored Core VI)
  xmlattr  — XML з атрибутами: <Shadows Quality="Medium"/>, SSAOQuality="2" (ELEX тощо)
  kvquoted — рядки «Ключ "значення"» (Diablo IV та інші ігри Blizzard)
"""
import glob
import os
import re

import autofps_common as C

STEPS = 12

# ---------------------------------------------------------------- що є налаштуванням якості

EXCLUDE = ("upscal", "dlss", "fsr", "xess", "sharp", "audio", "sound", "volume", "bright",
           "gamma", "hdr", "fov", "sensitiv", "resolution", "width", "height", "window", "vsync",
           "fps", "framerate", "frame", "monitor", "adapter", "font", "subtitle", "language",
           "colorblind", "cursor", "safezone", "hardwareclass", "pci", "version", "autodetect",
           "screenmode", "displaymode", "nits", "whitepoint", "blackpoint", "exposure", "gamepad",
           "mouse", "invert", "chroma", "scale", "physics", "population", "crowd", "traffic",
           "preset", "qualitysetting", "mixer", "dynamicres")

# ряд (A — знижується першим, C — останнім) за ключовими словами в назві
TIER_WORDS = {
    "A": ("shadow", "volumetric", "fog", "cloud", "reflect", "ssr", "ssao", "ambientocclusion",
          "occlusion", "globalillum", "gi", "lighting", "raytrac", "effect", "particle",
          "postprocess", "depthoffield", "dof", "motionblur", "bloom", "hair", "water", "caustic",
          "godray", "lensflare", "shade", "subsurface", "tessellat"),
    "B": ("lod", "viewdistance", "viewrange", "drawdistance", "distance", "foliage", "vegetation",
          "grass", "landscape", "terrain", "object", "mesh", "detail", "material", "environment",
          "sky", "clutter", "geometry", "decal", "antialias"),
    "C": ("texture", "anisotrop", "filter"),
}
# на яких сходинках ряд знижується від максимуму до мінімуму
TIER_SPAN = {"A": (1, 7), "B": (4, 10), "C": (9, 11)}

# смак гравця, а не якість: вмикати / підвищувати ніколи не будемо, лише знижувати
PREFERENCE = ("motionblur", "depthoffield", "dof", "chromatic", "vignett", "filmgrain",
              "lensflare", "lensdirt")

STRING_SCALES = [
    ["off", "low", "medium", "high", "ultra", "max"],
    ["low", "medium", "high", "ultra"],
    ["low", "medium", "high", "max"],
    ["low", "mid", "high", "max"],
    ["low", "medium", "high", "veryhigh", "ultra"],
    ["disable", "low", "medium", "high", "max"],
]
TOGGLES = [["off", "on"], ["disable", "enable"], ["false", "true"], ["0", "1"]]
TOGGLE_HEAVY = ("ssao", "ambientocclusion", "depthoffield", "dof", "motionblur", "volumetric",
                "raytrac", "bloom", "godray", "lensflare")


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def tier_of(name):
    n = _norm(name)
    for t in ("C", "A", "B"):          # «TextureQuality» — C, хоч і містить інші слова
        if any(w in n for w in TIER_WORDS[t]):
            return t
    return "B" if n.endswith("quality") else None


def scale_of(name, value):
    """Шкала значень (від найнижчого до найвищого) або None — не налаштування якості."""
    n = _norm(name)
    if any(w in n.replace("volumetric", "") for w in EXCLUDE):   # volume — звук, volumetric — графіка
        return None
    tier = tier_of(name)
    if tier is None:
        return None
    v = _norm(str(value))
    if "anisotrop" in n and v.isdigit():
        return ["1", "2", "4", "8", "16"] if v in ("1", "2", "4", "8", "16") else None
    if re.fullmatch(r"\d", v):
        if n.endswith("quality") or "quality" in n:
            return [str(i) for i in range(0, max(3, int(v)) + 1)]
        return None
    for sc in STRING_SCALES:
        if v in sc:
            return sc
    if any(w in n for w in TOGGLE_HEAVY):
        for sc in TOGGLES:
            if v in sc:
                return sc
    return None


# ---------------------------------------------------------------- формати файлів

class _Fmt:
    def __init__(self, path):
        self.path = path

    def scale(self, key, value):
        """(шкала, відома_повністю). Відома — можна ставити будь-яке значення шкали;
        інакше — не вище за найвище, що гра сама записувала у файл."""
        sc = scale_of(key, value)
        if not sc:
            return None, False
        # перемикачі й анізотропія — стандартні значення, решта — лише до баченого максимуму
        return sc, (sc in TOGGLES or sc == ["1", "2", "4", "8", "16"])

    def items(self):
        """[(ключ, значення)] у порядку файлу."""
        raise NotImplementedError

    def write(self, values):
        raise NotImplementedError


class FromSoftXml(_Fmt):
    name = "fromsoft"
    enc = "utf-16"
    TAG = re.compile(r"<([A-Za-z][\w\-]*)>([^<]*)</\1>")
    BASE = ["low", "medium", "high", "max"]

    def scale(self, key, value):
        # FromSoftware: LOW / MEDIUM / HIGH / MAX (+ OFF/DISABLE у деяких ефектів)
        if not scale_of(key, value):
            return None, False
        v = _norm(value)
        if v in self.BASE:
            return self.BASE, True
        if v in ("off", "disable"):
            return [v] + self.BASE, True
        return None, False

    def _text(self):
        with open(self.path, "rb") as f:
            raw = f.read()
        for enc in ("utf-16", "utf-8-sig", "utf-8"):
            try:
                t = raw.decode(enc)
                if "<config" in t or "<Config" in t:
                    self.enc = enc
                    return t
            except UnicodeError:
                continue
        return ""

    def items(self):
        return [(m.group(1), m.group(2).strip()) for m in self.TAG.finditer(self._text())]

    def write(self, values):
        text = self._text()

        def sub(m):
            k = m.group(1)
            if k in values:
                return f"<{k}>{values[k]}</{k}>"
            if k == "QualitySetting":      # власні налаштування замість пресету
                return f"<{k}>CUSTOM</{k}>"
            if k == "Auto-detectBestRenderingSettings":
                return f"<{k}>OFF</{k}>"
            return m.group(0)
        _save(self.path, self.TAG.sub(sub, text), self.enc)


class XmlAttr(_Fmt):
    name = "xmlattr"
    ELEM = re.compile(r"<([A-Za-z][\w\-]*)(\s[^<>]*?)/?>", re.S)
    ATTR = re.compile(r"([A-Za-z][\w\-]*)=\"([^\"]*)\"")

    def _text(self):
        with open(self.path, "rb") as f:
            raw = f.read()
        self.enc = "latin-1" if b"ISO-8859-1" in raw[:120] else "utf-8"
        return raw.decode(self.enc, errors="replace")

    def items(self):
        out = []
        for m in self.ELEM.finditer(self._text()):
            for a in self.ATTR.finditer(m.group(2)):
                out.append((f"{m.group(1)}.{a.group(1)}", a.group(2)))
        return out

    def write(self, values):
        text = self._text()

        def sub_el(m):
            el = m.group(1)

            def sub_at(a):
                k = f"{el}.{a.group(1)}"
                return f'{a.group(1)}="{values[k]}"' if k in values else a.group(0)
            return m.group(0).replace(m.group(2), self.ATTR.sub(sub_at, m.group(2)))
        _save(self.path, self.ELEM.sub(sub_el, text), self.enc)


class KvQuoted(_Fmt):
    name = "kvquoted"
    LINE = re.compile(r'^(\s*)([A-Za-z][\w.]*)(\s+)"([^"]*)"(.*)$')

    def items(self):
        out = []
        for line in C.read_text(self.path).splitlines():
            m = self.LINE.match(line)
            if m:
                out.append((m.group(2), m.group(4)))
        return out

    def write(self, values):
        with open(self.path, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        out = []
        for line in lines:
            m = self.LINE.match(line.rstrip("\r\n"))
            if m and m.group(2) in values:
                end = "\r\n" if line.endswith("\r\n") else "\n"
                line = f'{m.group(1)}{m.group(2)}{m.group(3)}"{values[m.group(2)]}"{m.group(5)}{end}'
            out.append(line)
        with open(self.path, "w", encoding="utf-8") as f:
            f.writelines(out)


def _save(path, text, enc):
    tmp = path + ".autofps.tmp"
    with open(tmp, "w", encoding=enc, newline="") as f:
        f.write(text)
    os.replace(tmp, path)


# де шукати (відносно steamuser у префіксі) → формат
LOCATIONS = [
    ("AppData/Roaming/*/GraphicsConfig.xml", FromSoftXml),
    ("AppData/Roaming/*/*/GraphicsConfig.xml", FromSoftXml),
    ("AppData/Local/*/Config/ConfigUser.xml", XmlAttr),
    ("Documents/*/LocalPrefs.txt", KvQuoted),
]


# ---------------------------------------------------------------- адаптер для autotune

class GenericAdapter:
    engine = "generic"
    lowres_supported = False

    def __init__(self, install, compat, device, fmt_cls=None, path=None):
        self.install, self.compat, self.device = install, compat, device
        self.fmt_cls, self.cfg = fmt_cls, path

    @staticmethod
    def detect(install):
        return False                       # вибирається через find() за файлами в префіксі

    @classmethod
    def find(cls, install, compat, device):
        root = os.path.join(compat, "pfx", "drive_c", "users", "steamuser")
        best = None
        for pat, fmt in LOCATIONS:
            for p in glob.glob(os.path.join(root, pat)):
                try:
                    n = len(cls._settings_of(fmt(p)))
                except (OSError, ValueError):
                    continue
                if n >= 2 and (best is None or os.path.getmtime(p) > os.path.getmtime(best[1])):
                    best = (fmt, p)
        if not best:
            return None
        a = cls(install, compat, device, *best)
        a.engine = f"generic:{best[0].name}"
        return a

    @staticmethod
    def _settings_of(fmt):
        out = {}
        for k, v in fmt.items():
            sc, known = fmt.scale(k, v)
            if sc and _norm(v) in sc:
                out[k] = (sc, tier_of(k), known)
        return out

    def update_max(self, xm):
        """Запам'ятати найвище значення кожного налаштування, що стояло у файлі (стеля)."""
        for k, (sc, _t, _known) in self._settings_of(self.fmt_cls(self.cfg)).items():
            v = _norm(dict(self.fmt_cls(self.cfg).items())[k])
            if v in sc:
                old = xm.get(k)
                if old is None or (old in sc and sc.index(v) > sc.index(old)):
                    xm[k] = v
        self.extra_max = xm

    def locate(self):
        return self.cfg

    def read(self, cfg):
        fmt = self.fmt_cls(cfg)
        known = self._settings_of(fmt)
        return {k: v for k, v in fmt.items() if k in known}

    def ladder(self, original, lowres=None):
        fmt = self.fmt_cls(self.cfg)
        settings = self._settings_of(fmt)
        cur = dict(fmt.items())
        steps = []
        for i in range(STEPS):
            st = {}
            seen = getattr(self, "extra_max", {}) or {}
            for k, (sc, tier, known) in settings.items():
                lo, hi = TIER_SPAN[tier]
                frac = min(1.0, max(0.0, (i - lo + 1) / float(hi - lo + 1)))
                if known and not any(w in _norm(k) for w in PREFERENCE):
                    cap = len(sc) - 1
                else:                       # не вище за те, що гра сама колись записала
                    vals = [x for x in (seen.get(k), _norm(cur.get(k, ""))) if x in sc]
                    cap = max(sc.index(x) for x in vals) if vals else 0
                # тіні/світло не вимикаємо зовсім — «низькі», а не «вимк.» (крім перемикачів)
                pref = any(w in _norm(k) for w in PREFERENCE)
                floor = 1 if sc[0] in ("off", "disable") and len(sc) > 2 and not pref else 0
                floor = min(floor, cap)
                idx = max(floor, int(round(cap - frac * (cap - floor))))
                st[k] = _cased(sc[idx], cur.get(k, ""))
            steps.append(st)
        return steps

    def rank(self, values, ladder):
        return ladder.index(values) if values in ladder else 0

    def res_scale(self, values):
        return 100

    def write(self, cfg, values):
        from autotune import backup_once
        backup_once(cfg)
        self.fmt_cls(cfg).write(values)

    def restore(self, cfg, original):
        self.write(cfg, original)

    def comparable(self, values):
        return dict(values)

    def describe(self, values):
        lv = {"A": [], "B": [], "C": []}
        settings = self._settings_of(self.fmt_cls(self.cfg))
        for k, v in values.items():
            if k in settings:
                sc, t, _known = settings[k]
                lv[t].append(sc.index(_norm(v)) / max(1, len(sc) - 1))

        def word(xs):
            if not xs:
                return "—"
            f = sum(xs) / len(xs)
            return "мін." if f < 0.2 else "низькі" if f < 0.45 else "середні" if f < 0.7 else "високі" if f < 0.95 else "макс."
        return f"ефекти й тіні {word(lv['A'])}, деталізація {word(lv['B'])}, текстури {word(lv['C'])}"

    def max_values(self):
        """Поточні значення як «стеля» (для extra_max)."""
        return {k: _norm(v) for k, v in self.read(self.cfg).items()}


def _cased(val, like):
    """Значення в регістрі, який використовує гра (MAX / Medium / medium)."""
    like = str(like)
    if like.isupper():
        return val.upper()
    if like[:1].isupper():
        return {"veryhigh": "VeryHigh"}.get(val, val.capitalize())
    return val
