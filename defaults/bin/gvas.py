"""Unreal Engine SaveGame (.sav, GVAS) — читання й точкове редагування налаштувань.

Багато ігор на Unreal тримають графіку не в GameUserSettings.ini, а в бінарному сейві
(Oblivion Remastered — Save_Settings.sav, Mortal Shell 2 — SpartaGameSettings.sav,
Portal Wars 2 — CloudSettings-*.sav тощо). Модуль розбирає дерево властивостей і змінює
окремі значення на місці, перераховуючи розміри всіх батьківських блоків, якщо довжина
значення змінилась. Решта файлу лишається байт-у-байт.

Формат:
  заголовок (необов'язковий): "GVAS", SaveGameVersion u32, PackageVersion u32,
    [UE5 PackageVersion u32 якщо SaveGameVersion ≥ 3], EngineVersion (u16×3, u32, FString),
    CustomVersionFormat u32, кількість + (GUID 16 + u32), назва класу FString,
    [UE 5.5+: 1 байт]
  властивості до "None":
    до UE 5.4:  Name, Type (FString), Size i64, додаткові поля за типом, HasGuid u8 [+16], тіло
    UE 5.4+:    Name, дерево типу (FString + i32 кількість дітей, рекурсивно), Size i32,
                Flags u8 (1 ArrayIndex i32, 2 Guid 16, 4 Extensions u8, 16 — значення Bool)
  FString: i32 довжина з нулем; > 0 — ASCII/Latin-1, < 0 — UTF-16 LE.

Джерела: uesave (trumank/uesave-rs), gvas (localcc/gvas), CoreUObject PropertyTag.cpp.
"""
import json
import re
import struct

NATIVE_STRUCTS = {"Vector", "Vector2D", "Vector4", "Rotator", "Quat", "Guid", "DateTime", "Timespan",
                  "LinearColor", "Color", "IntPoint", "IntVector", "Box", "Box2D", "Transform",
                  "SoftObjectPath", "SoftClassPath", "GameplayTagContainer", "UniqueNetIdRepl",
                  "FrameNumber", "Plane", "Matrix"}
SCALAR = {"IntProperty", "Int8Property", "Int16Property", "Int64Property", "UInt16Property",
          "UInt32Property", "UInt64Property", "FloatProperty", "DoubleProperty", "BoolProperty",
          "ByteProperty", "EnumProperty", "StrProperty", "NameProperty"}
FMT = {"IntProperty": "<i", "Int8Property": "<b", "Int16Property": "<h", "Int64Property": "<q",
       "UInt16Property": "<H", "UInt32Property": "<I", "UInt64Property": "<Q",
       "FloatProperty": "<f", "DoubleProperty": "<d"}


class GvasError(ValueError):
    pass


def enc_fstring(s):
    if s == "":
        return struct.pack("<i", 0)
    try:
        b = s.encode("ascii") + b"\0"
        return struct.pack("<i", len(b)) + b
    except UnicodeEncodeError:
        b = s.encode("utf-16-le") + b"\0\0"
        return struct.pack("<i", -(len(b) // 2)) + b


class Leaf:
    """Одне значення, яке можна змінити."""
    __slots__ = ("path", "kind", "value", "off", "length", "sizes", "flag_off", "json_key")

    def __init__(self, path, kind, value, off, length, sizes, flag_off=None, json_key=None):
        self.path, self.kind, self.value = path, kind, value
        self.off, self.length, self.sizes = off, length, sizes
        self.flag_off, self.json_key = flag_off, json_key


class Gvas:
    def __init__(self, data):
        self.d = bytes(data)
        self.p = 0
        self.leaves = []
        self.new_tags = False
        self.multi = False        # кілька блоків властивостей поспіль (DONTNOD)
        self._header()
        self._props(len(self.d), "", [])
        while self.multi and self.p + 8 < len(self.d):
            # між блоками — i32 (0); далі наступний блок властивостей
            self.p += 4
            before = len(self.leaves)
            try:
                self._props(len(self.d), "", [])
            except (GvasError, struct.error, IndexError):
                break
            if len(self.leaves) == before:
                break

    # ---------------------------------------------------------------- примітиви
    def _u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def _i32(self):
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def _str(self):
        n = self._i32()
        if n == 0:
            return ""
        if n > 0:
            if n > 1 << 16:
                raise GvasError("довгий рядок")
            s = self.d[self.p:self.p + n - 1].decode("latin-1")
            self.p += n
            return s
        n = -n
        if n > 1 << 16:
            raise GvasError("довгий рядок")
        s = self.d[self.p:self.p + 2 * n - 2].decode("utf-16-le", errors="replace")
        self.p += 2 * n
        return s

    # ---------------------------------------------------------------- заголовок
    def _header(self):
        if self.d[:8] == b"@DNESAV@":
            # DONTNOD (Banishers, Life is Strange): "@DNESAV@", u32 версія, u32, клас FString,
            # u32, версія рушія FString — далі звичайні властивості (класичні теги)
            self.p = 16
            self._str()
            self.p += 4
            self._str()
            self.multi = True
            return
        if self.d[:4] != b"GVAS":
            # деякі ігри (Oblivion Remastered) пишуть лише властивості, без заголовка;
            # інші — свій заголовок студії: шукаємо початок потоку властивостей самі
            if not self._looks_like_prop(0):
                start = self._find_props()
                if start is None:
                    raise GvasError("не схоже на сейв Unreal")
                self.p = start
                self.multi = True
            return
        self.p = 4
        sgv = struct.unpack_from("<I", self.d, 4)[0]
        self.p = 12
        ue5 = None
        if sgv >= 3:
            ue5 = struct.unpack_from("<I", self.d, 12)[0]
            self.p = 16
        self.p += 6 + 4          # EngineVersion: major, minor, patch u16 + changelist u32
        self._str()              # гілка
        self.p += 4              # CustomVersionFormat
        n = self._i32()
        if not 0 <= n < 4096:
            raise GvasError("заголовок")
        self.p += n * 20
        self._str()              # клас SaveGame
        self.new_tags = bool(ue5 and ue5 >= 1012)   # PROPERTY_TAG_COMPLETE_TYPE_NAME (UE 5.4)
        if ue5 and ue5 >= 1012 and self.p < len(self.d) and self.d[self.p] == 0:
            self.p += 1          # UE 5.5+: прапорці після назви класу

    PROP_TYPE = re.compile(rb"[\x05-\x20]\x00\x00\x00[A-Z][A-Za-z0-9]{2,30}Property\x00")

    def _looks_like_prop(self, at):
        """На позиції at — назва властивості (FString), а за нею тип «…Property»?"""
        try:
            n = struct.unpack_from("<i", self.d, at)[0]
            if not 2 <= n <= 128 or self.d[at + 4 + n - 1] != 0:
                return False
            name = self.d[at + 4:at + 4 + n - 1]
            if not re.fullmatch(rb"[A-Za-z_][A-Za-z0-9_.\-]*", name):
                return False
            return bool(self.PROP_TYPE.match(self.d, at + 4 + n))
        except (struct.error, IndexError):
            return False

    def _find_props(self):
        """Невідомий заголовок (власна обгортка студії): перша позиція, з якої
        читається властивість «Назва + …Property»."""
        for m in self.PROP_TYPE.finditer(self.d, 0, 4096):
            end = m.start()                       # тут починається FString типу
            for n in range(2, 129):               # назва перед ним: i32 n + n байтів
                at = end - 4 - n
                if at >= 0 and self._looks_like_prop(at):
                    return at
        return None

    # ---------------------------------------------------------------- властивості
    def _type_tree(self):
        name = self._str()
        n = self._i32()
        if not 0 <= n < 16:
            raise GvasError("дерево типу")
        return [name] + [self._type_tree() for _ in range(n)]

    def _props(self, end, prefix, sizes):
        while self.p < end:
            name = self._str()
            if name == "None" or name == "":
                return
            if self.new_tags:
                self._prop_new(name, prefix, sizes)
            else:
                self._prop_old(name, prefix, sizes)

    def _prop_old(self, name, prefix, sizes):
        typ = self._str()
        size_off = self.p
        size = struct.unpack_from("<q", self.d, self.p)[0]
        self.p += 8
        if not 0 <= size <= len(self.d):
            raise GvasError(f"розмір {name}")
        path = f"{prefix}{name}"
        my = sizes + [(size_off, 8)]
        if typ == "BoolProperty":
            off = self.p
            v = self._u8()
            if self._u8():
                self.p += 16
            self.leaves.append(Leaf(path, "bool_byte", bool(v), off, 1, []))
            return
        extra = {}
        if typ == "StructProperty":
            extra["struct"] = self._str()
            self.p += 16
        elif typ in ("ByteProperty", "EnumProperty"):
            extra["enum"] = self._str()
        elif typ in ("ArrayProperty", "SetProperty"):
            extra["inner"] = self._str()
        elif typ == "MapProperty":
            extra["key"], extra["value"] = self._str(), self._str()
        if self._u8():
            self.p += 16
        self._body(path, typ, extra, size, my, None)

    def _prop_new(self, name, prefix, sizes):
        tree = self._type_tree()
        typ = tree[0]
        size_off = self.p
        size = self._i32()
        flag_off = self.p
        flags = self._u8()
        if flags & 1:
            self.p += 4
        if flags & 2:
            self.p += 16
        if flags & 4:
            ext = self._u8()
            if ext & 2:
                self.p += 1
        if not 0 <= size <= len(self.d):
            raise GvasError(f"розмір {name}")
        path = f"{prefix}{name}"
        if typ == "BoolProperty":
            self.leaves.append(Leaf(path, "bool_flag", bool(flags & 16), flag_off, 1, [], flag_off))
            self.p += size
            return
        extra = {}
        kids = tree[1:]
        if typ == "StructProperty" and kids:
            extra["struct"] = kids[0][0]
        elif typ in ("ByteProperty", "EnumProperty"):
            extra["enum"] = kids[0][0] if kids else "None"
        elif typ in ("ArrayProperty", "SetProperty") and kids:
            extra["inner"] = kids[0][0]
        elif typ == "MapProperty" and len(kids) >= 2:
            extra["key"], extra["value"] = kids[0][0], kids[1][0]
        self._body(path, typ, extra, size, sizes + [(size_off, 4)], flags)

    def _body(self, path, typ, extra, size, sizes, flags):
        start, end = self.p, self.p + size
        try:
            if typ in FMT:
                v = struct.unpack_from(FMT[typ], self.d, start)[0]
                self.leaves.append(Leaf(path, typ, v, start, size, sizes))
            elif typ == "ByteProperty" and extra.get("enum") in ("None", "") and size == 1:
                self.leaves.append(Leaf(path, "byte", self.d[start], start, 1, sizes))
            elif typ in ("ByteProperty", "EnumProperty", "StrProperty", "NameProperty"):
                v = self._str()
                self.leaves.append(Leaf(path, "str", v, start, self.p - start, sizes))
                if typ == "StrProperty" and v.lstrip().startswith("{"):
                    self._json_leaves(path, v, start, self.p - start, sizes)
            elif typ == "StructProperty" and extra.get("struct") not in NATIVE_STRUCTS \
                    and not (flags and flags & 8):
                self.p = start
                self._props(end, f"{path}.", sizes)
            elif typ == "MapProperty":
                self._map(path, extra, end, sizes)
        except (GvasError, struct.error, IndexError, UnicodeDecodeError):
            pass                                  # незнайоме тіло — просто пропускаємо
        self.p = end

    def _map(self, path, extra, end, sizes):
        kt, vt = extra.get("key"), extra.get("value")
        simple = {"StrProperty", "NameProperty", "EnumProperty", "IntProperty", "FloatProperty",
                  "BoolProperty", "ByteProperty"}
        if kt not in ("StrProperty", "NameProperty", "IntProperty", "EnumProperty") or vt not in simple:
            return
        removed = self._i32()
        if removed:
            return
        n = self._i32()
        if not 0 <= n < 100000:
            return
        for _ in range(n):
            k = self._str() if kt != "IntProperty" else str(self._i32())
            off = self.p
            if vt in ("StrProperty", "NameProperty", "EnumProperty"):
                v = self._str()
                self.leaves.append(Leaf(f"{path}[{k}]", "str", v, off, self.p - off, sizes))
            elif vt == "IntProperty":
                self.leaves.append(Leaf(f"{path}[{k}]", "IntProperty", self._i32(), off, 4, sizes))
            elif vt == "FloatProperty":
                v = struct.unpack_from("<f", self.d, off)[0]
                self.p += 4
                self.leaves.append(Leaf(f"{path}[{k}]", "FloatProperty", v, off, 4, sizes))
            else:                                  # Bool / Byte — 1 байт
                self.leaves.append(Leaf(f"{path}[{k}]", "byte", self._u8(), off, 1, sizes))
            if self.p > end:
                raise GvasError("словник")

    JSON_KV = re.compile(r'"([^"\\]{1,120})"\s*:\s*(-?\d+(?:\.\d+)?|true|false|"[^"\\]{0,40}")')

    def _json_leaves(self, path, text, off, length, sizes):
        """JSON у рядку (Portal Wars 2: SerializedSettings) — кожне значення окремо."""
        try:
            json.loads(text)
        except ValueError:
            return
        counts = {}
        for m in self.JSON_KV.finditer(text):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        for m in self.JSON_KV.finditer(text):
            if counts[m.group(1)] != 1:
                continue                     # ключ повторюється в різних об'єктах — неоднозначно
            raw = m.group(2)
            v = raw[1:-1] if raw.startswith('"') else raw
            self.leaves.append(Leaf(f"{path}{{{m.group(1)}}}", "json", v, off, length, sizes,
                                    json_key=m.group(1)))

    # ---------------------------------------------------------------- значення
    @staticmethod
    def show(leaf):
        v = leaf.value
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, float):
            return str(int(v)) if v.is_integer() else f"{v:g}"
        return str(v)

    def items(self):
        out, seen = [], set()
        for lf in self.leaves:
            if lf.kind == "str" and any(x.kind == "json" and x.off == lf.off for x in self.leaves):
                continue                            # JSON-рядок віддаємо по ключах
            if lf.path in seen:
                continue
            seen.add(lf.path)
            out.append((lf.path, self.show(lf)))
        return out

    def patched(self, values):
        """Нові байти файлу зі зміненими значеннями {шлях: рядок}."""
        edits = {}                                    # off → (length, bytes, sizes)
        flagbits = []
        json_groups = {}
        for lf in self.leaves:
            if lf.path not in values or self.show(lf) == str(values[lf.path]):
                continue
            new = str(values[lf.path])
            if lf.kind == "bool_flag":
                on = new.lower() in ("true", "1", "on", "yes")
                flagbits.append((lf.flag_off, on))
            elif lf.kind == "bool_byte":
                edits[lf.off] = (1, bytes([1 if new.lower() in ("true", "1", "on", "yes") else 0]), [])
            elif lf.kind == "byte":
                edits[lf.off] = (1, bytes([int(float(new)) & 0xFF]), [])
            elif lf.kind in FMT:
                v = float(new) if lf.kind in ("FloatProperty", "DoubleProperty") else int(float(new))
                edits[lf.off] = (lf.length, struct.pack(FMT[lf.kind], v), [])
            elif lf.kind == "str":
                edits[lf.off] = (lf.length, enc_fstring(new), lf.sizes)
            elif lf.kind == "json":
                json_groups.setdefault(lf.off, (lf, {}))[1][lf.json_key] = new
        for off, (lf, kv) in json_groups.items():
            text = next(x.value for x in self.leaves if x.kind == "str" and x.off == off)

            def sub(m, kv=kv):
                if m.group(1) not in kv:
                    return m.group(0)
                old, new = m.group(2), kv[m.group(1)]
                rep = f'"{new}"' if old.startswith('"') else new
                return m.group(0)[:m.start(2) - m.start(0)] + rep
            edits[off] = (lf.length, enc_fstring(self.JSON_KV.sub(sub, text)), lf.sizes)
        out = bytearray(self.d)
        for off, on in flagbits:
            out[off] = (out[off] | 16) if on else (out[off] & ~16 & 0xFF)
        for off in sorted(edits, reverse=True):      # з кінця: попередні зсуви не зачіпаються
            length, data, sizes = edits[off]
            delta = len(data) - length
            out[off:off + length] = data
            if delta:
                for so, w in sizes:
                    f = "<q" if w == 8 else "<i"
                    struct.pack_into(f, out, so, struct.unpack_from(f, out, so)[0] + delta)
        return bytes(out)


def load(path):
    with open(path, "rb") as f:
        return Gvas(f.read())
