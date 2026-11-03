"""Універсальний підбір графіки для ігор не на Unreal / Unity / RE Engine.

1. Пошук: переглядає префікс Proton гри (AppData, Documents, Saved Games, реєстр user.reg)
   і теку гри, пробує розібрати кожен схожий файл усіма відомими форматами й бере той,
   у якому найбільше налаштувань якості графіки (шлях запам'ятовується).
2. Розбір: сам визначає, які налаштування — це якість графіки, і їхню шкалу значень.
3. Драбина з 12 сходинок: спершу дорогі для GPU й малопомітні ефекти (тіні, об'єм,
   відбиття, AO, трасування), потім деталізація й дальність, текстури — в останню чергу.
   Невідомі шкали — не вище за значення, яке гра сама колись записала; розмиття в русі,
   глибина різкості й «кіношні» ефекти ніколи не вмикаються.

Формати (рушій → приклади):
  fromsoft  GraphicsConfig.xml UTF-16 LOW/MEDIUM/HIGH/MAX     Elden Ring, Sekiro, DS3, AC6
  xmltext   <shadowQuality>kSettingLevel_High</shadowQuality>  RAGE (RDR2) та інші
  xmlattr   <TextureQuality value="2"/>, <attribute id= value=>, <X Quality="Medium"/>
            GTA V, Far Cry (gamerprofile.xml), Larian .lsx (BG3, DOS2), Codemasters, ELEX
  kvline    Key "v" / "Key" "v" / seta key "v" / Key v
            Blizzard (Diablo IV), Source 2 (CS2, Dota 2, Deadlock), id Tech (DOOM),
            Frostbite PROFSAVE_profile (Battlefield, Dragon Age, Dead Space), 4A user.cfg (Metro)
  ini       [Розділ] ключ=значення   Creation (Fallout 4/76, Skyrim), Ubisoft, Witcher 3
  json      {"name": .., "value": .., "options": [..]} та пласкі ключі
            REDengine 4 (Cyberpunk 2077), Remedy Northlight (Control, Alan Wake 2)
  func      Name(value)   Chrome Engine video.scr (Dying Light 1/2/The Beast)
  winreg    user.reg: HKCU\\Software\\<студія>\\<гра>   порти Nixxes, IOI, Unity PlayerPrefs
  gvas      бінарні сейви Unreal (.sav): властивості, словники, JSON у рядку (див. gvas.py)
  unityprefs  PlayerPrefs.data нових Unity (JSON ints/floats/bools/strings) із прапорцями меню
"""
import json
import os
import re

import autofps_common as C

STEPS = 12

# ---------------------------------------------------------------- що є налаштуванням якості

EXCLUDE = ("upscal", "dlss", "fsr", "xess", "sharp", "audio", "sound", "volume", "bright",
           "gamma", "hdr", "fov", "fieldofview", "sensitiv", "resolution", "width", "height",
           "window", "vsync", "fps", "framerate", "framegen", "frame", "monitor", "adapter", "font",
           "subtitle", "language", "colorblind", "cursor", "safezone", "hardwareclass", "pci",
           "version", "autodetect", "screenmode", "displaymode", "nits", "whitepoint", "blackpoint",
           "exposure", "gamepad", "mouse", "invert", "chroma", "scale", "physics", "population",
           "crowd", "traffic", "density", "preset", "qualitysetting", "mixer", "dynamicres",
           "latency", "reflex", "antilag", "camera", "hud", "ui", "menu", "tutorial", "difficulty",
           "voice", "music", "key", "bind", "controller", "vibration", "rumble", "aim", "speed",
           "timeout", "count", "index", "id", "seed", "time", "date", "playtime", "achievement",
           "fullscreen", "borderless", "refresh", "display", "output", "gpu", "cpu", "thread",
           "memory", "budget", "cache", "pool", "benchmark", "telemetry", "crash", "debug",
           "crosshair", "reticle", "marker", "show")
# «resolution» дозволена, якщо це роздільність ефекту, а не екрана
RES_OK = ("shadow", "reflect", "volumetric", "cloud", "fog", "texture", "ambientocclusion", "ssao")

# ряд (A — знижується першим, C — останнім) за ключовими словами в назві
TIER_WORDS = {
    "A": ("shadow", "volumetric", "fog", "cloud", "reflect", "ssr", "ssao", "hbao", "gtao",
          "ambientocclusion", "occlusion", "globalillum", "illumination", "lighting", "light",
          "raytrac", "pathtrac", "lumen", "effect", "particle", "postprocess", "postfx",
          "depthoffield", "dof", "motionblur", "bloom", "hair", "water", "caustic", "godray",
          "lensflare", "shade", "shader", "subsurface", "tessellat", "screenspace", "weather",
          "smoke", "fire", "explosion", "decal", "sss", "contact"),
    "B": ("lod", "viewdistance", "viewrange", "drawdistance", "distance", "foliage", "vegetation",
          "grass", "tree", "landscape", "terrain", "object", "mesh", "detail", "material",
          "environment", "sky", "clutter", "geometry", "antialias", "msaa", "taa", "smaa",
          "fxaa", "model", "character", "world", "streaming", "visual", "graphics", "render",
          "quality"),
    "C": ("texture", "texquality", "texres", "texdetail", "anisotrop", "filter"),
}
# на яких сходинках ряд знижується від максимуму до мінімуму
TIER_SPAN = {"A": (1, 9), "B": (3, 10), "C": (8, 11)}

# смак гравця, а не якість: вмикати / підвищувати ніколи не будемо, лише знижувати
PREFERENCE = ("motionblur", "depthoffield", "dof", "chromatic", "vignett", "filmgrain",
              "lensflare", "lensdirt", "lensdistort", "grain", "blur",
              # трасування на портативних — лише вимикаємо, ніколи не вмикаємо
              "raytrac", "pathtrac", "hardwarert", "rtgi", "rtao")

# шкали від найнижчого до найвищого; порядок — від найпоширенішої
STRING_SCALES = [
    ["low", "medium", "high", "ultra"],
    ["low", "medium", "high", "max"],
    ["low", "medium", "high", "veryhigh", "ultra"],
    ["low", "medium", "high", "epic", "cinematic"],
    ["low", "normal", "high", "ultra"],
    ["low", "mid", "high", "max"],
    ["verylow", "low", "medium", "high", "veryhigh", "ultra"],
    ["off", "low", "medium", "high", "ultra"],
    ["disable", "low", "medium", "high", "max"],
    ["low", "medium", "high", "ultra", "nightmare", "ultranightmare"],
    ["lowest", "low", "medium", "high", "highest"],
    ["minimum", "low", "medium", "high", "maximum"],
]
TOGGLES = [["off", "on"], ["disable", "enable"], ["disabled", "enabled"], ["false", "true"],
           ["0", "1"], ["no", "yes"]]
TOGGLE_HEAVY = ("ssao", "hbao", "gtao", "ambientocclusion", "depthoffield", "dof", "motionblur",
                "volumetric", "raytrac", "pathtrac", "bloom", "godray", "lensflare", "tessellat",
                "contactshadow", "screenspace", "ssr", "hair", "subsurface", "caustic", "fog",
                "cloud", "shadow", "reflection", "grass", "decal", "particle")
POW2 = ["512", "1024", "2048", "4096", "8192"]   # тіні нижче 512 — сходинки замість тіней
ANISO = ["1", "2", "4", "8", "16"]


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _core(v):
    """Значення без службових префіксів (RDR2: kSettingLevel_High → high, EQ::High → high)."""
    v = str(v).split("::")[-1]
    return re.sub(r"^(ksettinglevel|equality|quality|level|e)(?=(low|medium|high|ultra|off|on|max|normal|veryhigh|verylow|epic|special))",
                  "", _norm(v))


def _name(key):
    """Назва для аналізу: без суфікса Unity (_h12345) і службових префіксів."""
    n = re.sub(r"_h\d+$", "", str(key))
    return _norm(n)


def tier_of(name):
    n = _name(name)
    for t in ("C", "A", "B"):          # «TextureQuality» — C, хоч і містить інші слова
        if any(w in n for w in TIER_WORDS[t]):
            return t
    return None


def _excluded(n):
    m = n.replace("volumetric", "")
    for w in EXCLUDE:
        if w not in m:
            continue
        if w == "resolution" and any(r in m for r in RES_OK):
            continue
        if w in ("id", "ui", "key", "aim", "hud", "time", "count", "index", "gpu", "cpu", "fps"):
            # короткі слова — лише як окреме слово/закінчення, щоб не різати «fluid», «guide» тощо
            if not re.search(rf"(^|[^a-z]){w}($|[^a-z])", re.sub(r"([a-z])([A-Z])", r"\1 \2", m)) \
                    and not m.endswith(w) and not m.startswith(w):
                continue
        return True
    return False


def scale_of(name, value):
    """Шкала значень (від найнижчого до найвищого) або None — не налаштування якості."""
    n = _name(name)
    if not n or _excluded(n):
        return None
    tier = tier_of(name)
    if tier is None:
        return None
    raw = str(value).strip().strip('"')
    v = _core(raw)
    if "anisotrop" in n and v in ANISO:
        return ANISO
    if v in POW2 and any(w in n for w in ("shadowmap", "shadowres", "shadowsize", "resolution", "size")):
        return POW2
    if re.fullmatch(r"\d", v):
        if any(w in n for w in ("quality", "level", "detail", "lod")):
            return [str(i) for i in range(0, max(3, int(v)) + 1)]
        if v in ("0", "1") and any(w in n for w in TOGGLE_HEAVY):
            return ["0", "1"]
        return None
    for sc in STRING_SCALES:
        if v in sc:
            return sc
    if any(w in n for w in TOGGLE_HEAVY) and not ("shadow" in n and "contact" not in n):
        for sc in TOGGLES:            # тіні цілком не вимикаємо — лише контактні тіні
            if v in sc:
                return sc
    return None


def leaf_name(key):
    """Остання частина шляху: SavedSettings[Altar.GraphicsOptions.ShadowQuality] → ShadowQuality."""
    m = re.search(r"[\[{]([^\[\]{}]+)[\]}]$", key)
    k = m.group(1) if m else key
    return re.split(r"[./\\]", k)[-1]


UE_GROUPS = ("viewdistancequality", "antialiasingquality", "shadowquality", "globalilluminationquality",
             "reflectionquality", "postprocessquality", "texturequality", "effectsquality",
             "foliagequality", "shadingquality", "landscapequality")


def _cased(val, like):
    """Значення у вигляді, який використовує гра (MAX / Medium / kSettingLevel_High / 2)."""
    like = str(like)
    m = re.match(r"^([A-Za-z_]\w*::|kSettingLevel_|eQuality_|Quality_|Level_)", like)
    prefix = m.group(1) if m else ""
    core = like[len(prefix):]
    if core.isupper() and not core.isdigit():
        out = val.upper()
    elif core[:1].isupper():
        out = {"veryhigh": "VeryHigh", "verylow": "VeryLow", "ultranightmare": "UltraNightmare"}.get(
            val, val.capitalize())
    else:
        out = val
    return prefix + out


# ---------------------------------------------------------------- формати файлів

def _read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace"), "utf-16"
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1"


def _save(path, text, enc):
    tmp = path + ".autofps.tmp"
    with open(tmp, "w", encoding=enc, newline="") as f:
        f.write(text)
    os.replace(tmp, path)


class _Fmt:
    name = "?"

    def __init__(self, path):
        self.path = path

    def scale(self, key, value):
        """(шкала, відома_повністю). Відома — можна ставити будь-яке значення шкали;
        інакше — не вище за найвище, що гра сама записувала у файл."""
        sc = scale_of(key, value)
        if not sc:
            return None, False
        return sc, (sc in TOGGLES or sc == ANISO)

    def items(self):
        raise NotImplementedError

    def write(self, values):
        raise NotImplementedError


class XmlText(_Fmt):
    name = "xmltext"
    TAG = re.compile(r"<([A-Za-z][\w\-.]*)>([^<]{1,64})</\1>")

    def items(self):
        text, self.enc = _read_text(self.path)
        return [(m.group(1), m.group(2).strip()) for m in self.TAG.finditer(text)]

    def _sub(self, values):
        def sub(m):
            k = m.group(1)
            return f"<{k}>{values[k]}</{k}>" if k in values else m.group(0)
        return sub

    def write(self, values):
        text, enc = _read_text(self.path)
        _save(self.path, self.TAG.sub(self._sub(values), text), enc)


class FromSoftXml(XmlText):
    name = "fromsoft"
    BASE = ["low", "medium", "high", "max"]

    @classmethod
    def accepts(cls, path):
        try:
            t, _ = _read_text(path)
        except OSError:
            return False
        return "<config>" in t and "QualitySetting" in t

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

    def _sub(self, values):
        def sub(m):
            k = m.group(1)
            if k in values:
                return f"<{k}>{values[k]}</{k}>"
            if k == "QualitySetting":      # власні налаштування замість пресету
                return f"<{k}>CUSTOM</{k}>"
            if k == "Auto-detectBestRenderingSettings":
                return f"<{k}>OFF</{k}>"
            return m.group(0)
        return sub


class XmlAttr(_Fmt):
    """<X a="1" b="High"/>, <TextureQuality value="2"/> (GTA V), <attribute id="Shadow" value="2"/> (Larian)."""
    name = "xmlattr"
    ELEM = re.compile(r"<([A-Za-z][\w\-.:]*)(\s[^<>]*?)/?>", re.S)
    ATTR = re.compile(r"([A-Za-z][\w\-.:]*)=\"([^\"]*)\"")

    def _walk(self, text, values=None):
        out, seen = [], {}

        def keyed(k):
            seen[k] = seen.get(k, 0) + 1
            return k if seen[k] == 1 else f"{k}#{seen[k]}"

        def sub_el(m):
            el, body = m.group(1), m.group(2)
            attrs = dict(self.ATTR.findall(body))
            if "id" in attrs and "value" in attrs:              # Larian .lsx
                plan = {"value": keyed(attrs["id"])}
            elif "value" in attrs and len(attrs) <= 2:          # GTA V settings.xml
                plan = {"value": keyed(el)}
            else:
                plan = {a: keyed(f"{el}.{a}") for a in attrs}
            for a, k in plan.items():
                out.append((k, attrs[a]))
            if values is None:
                return m.group(0)

            def sub_at(am):
                k = plan.get(am.group(1))
                return f'{am.group(1)}="{values[k]}"' if k in values else am.group(0)
            return m.group(0).replace(body, self.ATTR.sub(sub_at, body))
        new = self.ELEM.sub(sub_el, text)
        return out, new

    def items(self):
        text, _ = _read_text(self.path)
        return self._walk(text)[0]

    def write(self, values):
        text, enc = _read_text(self.path)
        _save(self.path, self._walk(text, values)[1], enc)


class KvLine(_Fmt):
    """Рядки «ключ значення»: Key "v" | "Key" "v" | seta key "v" | Key v."""
    name = "kvline"
    LINE = re.compile(r'^(\s*)((?:seta|set)\s+)?("?)([A-Za-z_][\w.\-]*)\3([ \t]+)("?)([^"\s]{0,48})\6([ \t]*(?://.*)?)$')

    def items(self):
        text, self.enc = _read_text(self.path)
        out = []
        for line in text.splitlines():
            m = self.LINE.match(line)
            if m:
                out.append((m.group(4), m.group(7)))
        return out

    def write(self, values):
        text, enc = _read_text(self.path)
        lines = text.splitlines(keepends=True)
        out = []
        for line in lines:
            body = line.rstrip("\r\n")
            end = line[len(body):]
            m = self.LINE.match(body)
            if m and m.group(4) in values:
                q = m.group(6)
                body = (f"{m.group(1)}{m.group(2) or ''}{m.group(3)}{m.group(4)}{m.group(3)}"
                        f"{m.group(5)}{q}{values[m.group(4)]}{q}{m.group(8)}")
            out.append(body + end)
        _save(self.path, "".join(out), enc)


class IniFmt(_Fmt):
    name = "ini"
    SEC = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    KV = re.compile(r"^(\s*)([^=;#\[\s][^=]*?)(\s*=\s*)(\"?)([^\"\r\n]{0,48})\4(\s*)$")

    def _iter(self, text):
        sec = ""
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            ms = self.SEC.match(body)
            if ms:
                sec = ms.group(1).strip()
                yield line, None, None
                continue
            m = self.KV.match(body)
            yield line, (f"{sec}/{m.group(2).strip()}" if m else None), m

    def items(self):
        text, self.enc = _read_text(self.path)
        return [(k, m.group(5).strip()) for _l, k, m in self._iter(text) if k]

    def scale(self, key, value):
        # аналізуємо назву ключа, розділ — як підказку ряду (напр. [Shadows] Quality=2)
        sec, _, k = key.rpartition("/")
        k2 = re.sub(r"^[ibfsu](?=[A-Z])", "", k)           # угорська нотація Creation: iShadow…
        sc = scale_of(k2, value) or (scale_of(f"{sec}{k2}", value) if sec else None)
        if not sc:
            return None, False
        return sc, (sc in TOGGLES or sc == ANISO)

    def write(self, values):
        text, enc = _read_text(self.path)
        out = []
        for line, k, m in self._iter(text):
            if k and k in values:
                body = line.rstrip("\r\n")
                end = line[len(body):]
                line = f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}{values[k]}{m.group(4)}{m.group(6)}{end}"
            out.append(line)
        _save(self.path, "".join(out), enc)


class JsonFmt(_Fmt):
    name = "json"

    def _load(self):
        text, self.enc = _read_text(self.path)
        self.indent = "\t" if "\n\t" in text else (4 if "\n    " in text else 2)
        return json.loads(text)

    def _walk(self, node, path, out, group=""):
        if isinstance(node, dict):
            g = node.get("group_name") if isinstance(node.get("group_name"), str) else group
            if "name" in node and "value" in node and isinstance(node["name"], str) \
                    and isinstance(node["value"], (str, int, bool)):
                opts = node.get("options") or node.get("values") or node.get("listItems")
                scale = None
                if isinstance(opts, list) and opts and all(isinstance(o, (str, int)) for o in opts):
                    scale = [_core(o) for o in opts]
                elif isinstance(node.get("min_value"), int) and isinstance(node.get("max_value"), int) \
                        and isinstance(node["value"], int) and node["max_value"] - node["min_value"] <= 8:
                    scale = [str(i) for i in range(node["min_value"], node["max_value"] + 1)]
                out.append((f"{g}/{node['name']}" if g else node["name"], node, "value", scale))
                return
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    self._walk(v, f"{path}.{k}" if path else k, out, g)
                elif isinstance(v, (str, int, bool)) and not isinstance(v, float):
                    out.append((f"{path}.{k}" if path else k, node, k, None))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, (dict, list)):
                    self._walk(v, f"{path}[{i}]", out, group)

    def _entries(self):
        data = self._load()
        out = []
        self._walk(data, "", out)
        return data, out

    @staticmethod
    def _val(v):
        return ("true" if v else "false") if isinstance(v, bool) else str(v)

    def items(self):
        _d, ents = self._entries()
        self._scales = {k: s for k, _n, _f, s in ents if s}
        return [(k, self._val(n[f])) for k, n, f, _s in ents]

    def scale(self, key, value):
        key_name = key.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
        explicit = getattr(self, "_scales", {}).get(key)
        if explicit:
            if scale_of(key_name, explicit[-1]) is None and scale_of(key_name, value) is None \
                    and tier_of(key_name) is None:
                return None, False
            if _excluded(_name(key_name)):
                return None, False
            return explicit, True                  # гра сама перелічила допустимі значення
        sc = scale_of(key_name, value)
        if not sc:
            return None, False
        return sc, (sc in TOGGLES or sc == ANISO)

    def write(self, values):
        data, ents = self._entries()
        for k, node, field, scale in ents:
            if k not in values:
                continue
            old, new = node[field], values[k]
            if isinstance(old, bool):
                node[field] = _norm(new) in ("true", "1", "on", "enable", "enabled", "yes")
            elif isinstance(old, int):
                node[field] = int(new)
            elif scale and isinstance(node.get("options"), list):
                match = [o for o in node["options"] if _core(o) == _core(new)]
                node[field] = match[0] if match else new
            else:
                node[field] = new
        _save(self.path, json.dumps(data, indent=self.indent, ensure_ascii=False), self.enc)


class FuncFmt(_Fmt):
    """Chrome Engine video.scr: ShadowMapSize(2048) / TextureQuality("High")."""
    name = "func"
    LINE = re.compile(r'^(\s*)([A-Za-z_]\w*)\((\s*)("?)([^"(),]{0,32})\4(\s*)\)(\s*;?\s*(?://.*)?)$')

    def items(self):
        text, self.enc = _read_text(self.path)
        out = []
        for line in text.splitlines():
            m = self.LINE.match(line)
            if m:
                out.append((m.group(2), m.group(5).strip()))
        return out

    def write(self, values):
        text, enc = _read_text(self.path)
        out = []
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            end = line[len(body):]
            m = self.LINE.match(body)
            if m and m.group(2) in values:
                body = (f"{m.group(1)}{m.group(2)}({m.group(3)}{m.group(4)}{values[m.group(2)]}"
                        f"{m.group(4)}{m.group(6)}){m.group(7)}")
            out.append(body + end)
        _save(self.path, "".join(out), enc)


class WinReg(_Fmt):
    """Реєстр Wine (pfx/user.reg): лише HKCU\\Software\\<студія>\\<гра>, не системні гілки."""
    name = "winreg"
    SEC = re.compile(r"^\[(Software\\\\[^\]]+)\]")
    VAL = re.compile(r'^"([^"]+)"=(dword:([0-9a-fA-F]{8})|"([^"]{0,32})")\s*$')
    SKIP = ("software\\\\wine", "software\\\\microsoft", "software\\\\valve", "software\\\\classes",
            "software\\\\policies", "software\\\\khronos", "software\\\\nvidia", "software\\\\amd",
            "software\\\\ati", "software\\\\intel", "software\\\\epic games", "software\\\\electronic arts",
            "software\\\\ubisoft\\\\launcher", "software\\\\rockstar games\\\\launcher", "software\\\\blizzard",
            "software\\\\gog", "software\\\\codeweavers", "software\\\\unity\\\\unityeditor",
            "software\\\\unity technologies")

    def _iter(self, text):
        sec = None
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            if body.startswith("["):
                m = self.SEC.match(body)
                sec = m.group(1) if m and not m.group(1).lower().startswith(self.SKIP) else None
                yield line, None, None
                continue
            m = self.VAL.match(body) if sec else None
            yield line, (f"{sec}\\\\{m.group(1)}" if m else None), m

    @staticmethod
    def _v(m):
        return str(int(m.group(3), 16)) if m.group(3) else m.group(4)

    def items(self):
        text, self.enc = _read_text(self.path)
        return [(k, self._v(m)) for _l, k, m in self._iter(text) if k]

    def scale(self, key, value):
        sc = scale_of(key.rsplit("\\", 1)[-1], value)
        if not sc:
            return None, False
        return sc, (sc in TOGGLES or sc == ANISO)

    def write(self, values):
        text, enc = _read_text(self.path)
        out = []
        for line, k, m in self._iter(text):
            if k and k in values:
                end = line[len(line.rstrip("\r\n")):]
                if m.group(3):
                    line = f'"{m.group(1)}"=dword:{int(values[k]):08x}{end}'
                else:
                    line = f'"{m.group(1)}"="{values[k]}"{end}'
            out.append(line)
        _save(self.path, "".join(out), enc)


class GvasFmt(_Fmt):
    """Сейв Unreal (.sav, GVAS) — властивості, словники, JSON у рядку (див. gvas.py)."""
    name = "gvas"

    def _g(self):
        import gvas
        return gvas.load(self.path)

    def items(self):
        return self._g().items()

    def scale(self, key, value):
        leaf = leaf_name(key)
        v = _core(value)
        inner = re.search(r"[\[{]([^\[\]{}]+)[\]}]$", key)
        if _excluded(_name(inner.group(1) if inner else key)):
            return None, False           # Altar.DLSS.Quality, …Audio.Volume тощо
        # стандартні групи якості Unreal: 0 низькі … 3 епічні (4 — кіно, не ставимо)
        if _norm(leaf) in UE_GROUPS and v in ("0", "1", "2", "3", "4"):
            return ["0", "1", "2", "3"] if v != "4" else ["0", "1", "2", "3", "4"], True
        sc = scale_of(leaf, value)
        if not sc:
            return None, False
        return sc, (sc in TOGGLES or sc == ANISO)

    def write(self, values):
        data = self._g().patched(values)
        tmp = self.path + ".autofps.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, self.path)


OPTION_RANK = ["none", "off", "disabled", "verylow", "lowest", "low", "quarter", "half", "performance",
               "med", "mid", "medium", "normal", "balanced", "default", "high", "full", "quality",
               "veryhigh", "ultra", "epic", "max", "maximum", "cinematic"]


class UnityPrefsData(_Fmt):
    """Нові Unity: PlayerPrefs.data — JSON {"ints":{}, "floats":{}, "bools":{}, "strings":{}}.
    Багато ігор дублюють вибір у меню прапорцями: Vegetation=0 і Vegetation_low=true —
    шкалу беремо з цих прапорців, а при записі оновлюємо і число, і прапорці."""
    name = "unityprefs"

    def _load(self):
        text, self.enc = _read_text(self.path)
        d = json.loads(text)
        if not isinstance(d, dict) or not isinstance(d.get("ints"), dict):
            raise ValueError("не PlayerPrefs.data")
        return text, d

    def _options(self, d, key):
        opts = [k[len(key) + 1:] for k in d.get("bools", {}) if k.startswith(key + "_")]
        ranked = [o for o in opts if _norm(o) in OPTION_RANK]
        if len(ranked) < 2 or len(ranked) != len(opts):
            return None
        return sorted(ranked, key=lambda o: OPTION_RANK.index(_norm(o)))

    def items(self):
        _t, d = self._load()
        out = [(f"ints/{k}", str(v)) for k, v in d["ints"].items() if isinstance(v, int)]
        out += [(f"bools/{k}", "true" if v else "false") for k, v in d.get("bools", {}).items()
                if isinstance(v, bool) and not self._is_option_flag(d, k)]
        return out

    def _is_option_flag(self, d, k):
        base = k.rsplit("_", 1)[0]
        return "_" in k and base in d["ints"] and self._options(d, base) is not None

    def scale(self, key, value):
        sec, _, k = key.partition("/")
        _t, d = self._load()
        if sec == "ints":
            opts = self._options(d, k)
            if opts:
                chosen = [o for o in opts if d["bools"].get(f"{k}_{o}")]
                cur = int(d["ints"][k])
                # число має збігатися з позицією вибраного прапорця — інакше шкала невідома
                if chosen and opts.index(chosen[0]) == cur and not _excluded(_name(k)) and tier_of(k):
                    return [str(i) for i in range(len(opts))], False
                return None, False
        sc = scale_of(k, value)
        if not sc:
            return None, False
        return sc, sc in TOGGLES

    def write(self, values):
        text, d = self._load()
        for key, new in values.items():
            sec, _, k = key.partition("/")
            if sec == "ints":
                text = self._set(text, "ints", k, str(int(float(new))))
                opts = self._options(d, k)
                if opts and int(float(new)) < len(opts):
                    for i, o in enumerate(opts):
                        text = self._set(text, "bools", f"{k}_{o}", "true" if i == int(float(new)) else "false")
            elif sec == "bools":
                text = self._set(text, "bools", k, "true" if _norm(new) in ("true", "1", "on") else "false")
        json.loads(text)                     # перевірка, що JSON цілий
        _save(self.path, text, self.enc)

    @staticmethod
    def _set(text, section, key, value):
        m = re.search(r'"%s"\s*:\s*\{' % section, text)
        if not m:
            return text
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        block = text[m.end():i]
        pat = re.compile(r'("%s"\s*:\s*)(-?\d+(?:\.\d+)?|true|false|True|False)' % re.escape(key))
        nb, n = pat.subn(lambda mm: mm.group(1) + value, block, count=1)
        return text[:m.end()] + nb + text[i:] if n else text


FORMATS = {c.name: c for c in (FromSoftXml, XmlText, XmlAttr, KvLine, IniFmt, JsonFmt, FuncFmt, WinReg,
                               GvasFmt, UnityPrefsData)}

# ---------------------------------------------------------------- пошук файлу налаштувань

USER_DIRS = ["AppData/Local", "AppData/Roaming", "AppData/LocalLow", "Documents", "Saved Games"]
SKIP_DIRS = ("cache", "shader", "logs", "log", "crash", "temp", "tmp", "webcache", "d3dscache",
             "nvidia", "microsoft", "packages", "cef", "gpucache", "dumps", "screenshots",
             "ubisoft game launcher", "electronic arts", "ea desktop", "origin", "battle.net",
             "epic", "steam", "gog.com", "galaxy", "unrealengine", "vkd3d", "dxvk", "mods",
             "music", "videos", "pictures", "my music", "my videos", "my pictures", "downloads",
             "fontconfig", "windows", "sentry", "telemetry", "analytics", "backup", "browser")
NAME_HINT = re.compile(r"(setting|config|option|pref|profile|video|graphic|render|display|user|system)", re.I)
# сейви з прогресом гри (великі) — не чіпаємо: лише файли налаштувань
SAV_MAX = 256 * 1024
EXT_FORMATS = {
    ".xml": ("fromsoft", "xmltext", "xmlattr"), ".lsx": ("xmlattr",), ".json": ("json",),
    ".ini": ("ini", "json", "kvline"), ".cfg": ("kvline", "ini", "json"), ".txt": ("kvline", "ini", "json"),
    ".local": ("kvline",), ".scr": ("func",), ".sav": ("gvas",), ".data": ("unityprefs", "json"), ".settings": ("ini", "json"), ".config": ("xmlattr", "ini", "json"),
    "": ("kvline", "ini", "json", "xmlattr"),
}
MAX_SIZE = 512 * 1024


def _candidates(compat, install):
    root = os.path.join(compat, "pfx", "drive_c", "users", "steamuser")
    bases = [(os.path.join(root, d), 6) for d in USER_DIRS]
    if install:
        bases.append((install, 4))
    for base, depth in bases:
        if not os.path.isdir(base):
            continue
        b0 = base.rstrip("/").count("/")
        for cur, dirs, files in os.walk(base):
            if cur.count("/") - b0 >= depth:
                dirs[:] = []
            dirs[:] = [d for d in dirs if not any(s == d.lower() or d.lower().startswith(s + " ")
                                                   or s in d.lower() for s in SKIP_DIRS)]
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in EXT_FORMATS or fn.endswith((".autofps.bak", ".autofps.tmp")):
                    continue
                if ext in ("", ".txt", ".cfg", ".json", ".config", ".ini", ".sav", ".data") \
                        and not NAME_HINT.search(fn) \
                        and fn != "PROFSAVE_profile":
                    continue
                p = os.path.join(cur, fn)
                try:
                    if os.path.getsize(p) <= (SAV_MAX if ext == ".sav" else MAX_SIZE):
                        yield p, ext
                except OSError:
                    pass
    reg = os.path.join(compat, "pfx", "user.reg")
    if os.path.isfile(reg):
        yield reg, ".reg"


def _try(fmt_name, path):
    cls = FORMATS[fmt_name]
    if cls is FromSoftXml and not FromSoftXml.accepts(path):
        return 0
    try:
        return len(GenericAdapter._settings_of(cls(path)))
    except Exception:  # noqa: BLE001 — будь-який зламаний файл просто пропускаємо
        return 0


def discover(compat, install):
    """(формат, шлях) файлу з найбільшою кількістю налаштувань якості (≥ 3) або None."""
    best = None
    for path, ext in _candidates(compat, install):
        names = ("winreg",) if ext == ".reg" else EXT_FORMATS[ext]
        for fn in names:
            n = _try(fn, path)
            if n < 3:
                continue
            try:
                mt = os.path.getmtime(path)
            except OSError:
                mt = 0
            # реєстр — лише якщо ніде немає файлу (там і чужі програми бувають)
            score = (n - (2 if fn == "winreg" else 0), mt)
            if best is None or score > best[0]:
                best = (score, fn, path)
            break
    return (best[1], best[2]) if best else None


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
    def find(cls, install, compat, device, cached=None):
        """cached = (назва_формату, шлях) з минулого запуску — без повного пошуку."""
        hit = None
        if cached and cached[0] in FORMATS and os.path.isfile(cached[1]) and _try(*cached) >= 3:
            hit = cached
        if not hit:
            hit = discover(compat, install)
        if not hit:
            return None
        a = cls(install, compat, device, FORMATS[hit[0]], hit[1])
        a.engine = f"generic:{hit[0]}"
        a.fmt_name = hit[0]
        return a

    @staticmethod
    def _settings_of(fmt):
        out = {}
        for k, v in fmt.items():
            sc, known = fmt.scale(k, v)
            if sc and _core(v) in sc:
                t = tier_of(k.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]) or tier_of(k) or "B"
                out[k] = (sc, t, known)
        return out

    def update_max(self, xm):
        """Запам'ятати найвище значення кожного налаштування, що стояло у файлі (стеля)."""
        fmt = self.fmt_cls(self.cfg)
        cur = dict(fmt.items())
        for k, (sc, _t, _known) in self._settings_of(fmt).items():
            v = _core(cur[k])
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
        seen = getattr(self, "extra_max", {}) or {}
        steps = []
        for i in range(STEPS):
            st = {}
            for k, (sc, tier, known) in settings.items():
                lo, hi = TIER_SPAN[tier]
                frac = min(1.0, max(0.0, (i - lo + 1) / float(hi - lo + 1)))
                pref = any(w in _name(k) for w in PREFERENCE)
                if known and not pref:
                    cap = len(sc) - 1
                else:                       # не вище за те, що гра сама колись записала
                    vals = [x for x in (seen.get(k), _core(cur.get(k, ""))) if x in sc]
                    cap = max(sc.index(x) for x in vals) if vals else 0
                # тіні/світло не вимикаємо зовсім — «низькі», а не «вимк.» (крім перемикачів)
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
                if _core(v) in sc:
                    lv[t].append(sc.index(_core(v)) / max(1, len(sc) - 1))

        def word(xs):
            if not xs:
                return "—"
            f = sum(xs) / len(xs)
            return "мін." if f < 0.2 else "низькі" if f < 0.45 else "середні" if f < 0.7 else "високі" if f < 0.95 else "макс."
        return (f"{len(settings)} налашт.: ефекти й тіні {word(lv['A'])}, деталізація {word(lv['B'])}, "
                f"текстури {word(lv['C'])}")
