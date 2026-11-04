#!/usr/bin/env python3
"""Самоперевірка підбору графіки по всіх встановлених іграх (лише читання, нічого не пише).

Для кожної гри: який рушій / файл налаштувань знайдено, які налаштування плагін змінює
і їхні значення на сходинках 1 / 4 / 7 / 10, а головне — графічні налаштування у файлі,
які лишились БЕЗ керування, і ті, що на середніх сходинках досі на максимумі.

  python3 settings_audit.py [steamapps …]
"""
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofps_common as C  # noqa: E402
import autotune as A  # noqa: E402
import generic_settings as G  # noqa: E402

LOOKS_GRAPHIC = re.compile(r"(quality|detail|shadow|textur|reflect|lighting|illumin|occlusion|ssao|"
                           r"fog|cloud|volumetric|foliage|grass|vegetat|mesh|material|lod|distance|"
                           r"effect|particle|postprocess|bloom|aniso|tessell|raytrac|lumen|shading|"
                           r"water|hair|cloth|terrain|landscape|geometry|decal)", re.I)
NOT_GRAPHIC = re.compile(r"(audio|sound|volume(?!tric)|mouse|gamepad|controller|sensitiv|fov|hud|"
                         r"subtitle|font|language|culture|brightness|gamma|resolution(size)?[xy]|"
                         r"window|vsync|framerate|fps|upscal|dlss|fsr|xess|sharpen|benchmark|"
                         r"difficulty|vibration|haptic|colou?r|camera|crosshair|population|"
                         r"traffic|physics|monitor|display|hdr|nits)", re.I)
STEPS_SHOWN = (0, 3, 6, 9)


def raw_items(path, fmt_name=None):
    """Усі пари ключ-значення файлу (для пошуку некерованих налаштувань)."""
    if fmt_name:
        try:
            return dict(G.FORMATS[fmt_name](path).items())
        except Exception:  # noqa: BLE001
            return {}
    out, sec = {}, ""
    for line in C.read_text(path).splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            sec = s[1:-1]
        elif "=" in s and not s.startswith((";", "#")):
            k, v = s.split("=", 1)
            out[f"{sec}/{k.strip()}"] = v.strip()
    return out


def short(k):
    return G.leaf_name(k) if "[" in k or "{" in k else re.split(r"[/\\\\]", k)[-1]


def audit(appid, install, compat, device):

    adapter = None
    for Ad in A.ADAPTERS:
        if Ad.detect(install):
            adapter = Ad(install, compat, device)
            break
    generic = None
    try:
        generic = G.GenericAdapter.find(install, compat, device)
    except Exception:  # noqa: BLE001
        generic = None
    if adapter and adapter.engine == "unity" and generic and \
            len(generic._settings_of(generic.fmt_cls(generic.cfg))) >= 4:
        adapter = generic
    if not adapter:
        adapter = generic
    if not adapter:
        return {"engine": "—", "note": "файл налаштувань графіки не знайдено"}
    cfg = adapter.locate()
    if not cfg:
        return {"engine": adapter.engine, "note": "рушій визначено, файл налаштувань ще не створено"}
    res = {"engine": adapter.engine, "config": cfg}
    try:
        cur = adapter.read(cfg)
        if adapter.engine == "unreal":
            xm = {}
            for k, v in cur.items():
                if k.startswith("x.") or (k.startswith("sg.") and k not in A.UE_KEYS):
                    try:
                        xm[k] = int(float(v))
                    except ValueError:
                        pass
            adapter.extra_max = xm
        elif hasattr(adapter, "update_max"):
            adapter.update_max({})
        ladder = adapter.ladder(cur)
    except Exception as e:  # noqa: BLE001
        res["note"] = f"помилка розбору: {e!r}"
        return res
    managed = set()
    for st in ladder:
        managed |= set(st)
    res["steps"] = {short(k): [str(ladder[i].get(k, "")) for i in STEPS_SHOWN if i < len(ladder)]
                    for k in sorted(managed)}
    # що лишилось на максимумі на сходинці 7 (середина драбини)
    res["max_at_7"] = [short(k) for k in managed
                       if len(ladder) > 6 and str(ladder[6].get(k)) == str(ladder[0].get(k))
                       and str(ladder[0].get(k)) != str(ladder[-1].get(k))]
    # графічні налаштування у файлі, яких драбина не торкається
    fmt_name = getattr(adapter, "fmt_name", None)
    items = raw_items(cfg, fmt_name)
    names_managed = {short(k).lower() for k in managed}
    unmanaged = []
    for k, v in items.items():
        n = short(k)
        if n.lower() in names_managed or n.lower().startswith("sg.") and n.lower() in names_managed:
            continue
        if f"sg.{n}".lower() in names_managed or f"x.{n}".lower() in names_managed:
            continue
        if n in A.NOT_TUNED:
            continue
        if LOOKS_GRAPHIC.search(n) and not NOT_GRAPHIC.search(k) and len(str(v)) <= 24:
            unmanaged.append(f"{n}={v}")
    res["unmanaged"] = unmanaged
    if adapter.engine == "unreal":
        sav = adapter.settings_sav(cfg)
        if sav:
            res["sav"] = sav
        ov = adapter.overlays(cfg)
        if ov:
            res["overlays"] = ov
    return res


def main():
    home = C.HOME
    libs = sys.argv[1:] or [os.path.join(home, ".local/share/Steam/steamapps"), "/run/media/deck/SDCard/steamapps"]
    device = dict(C.detect_device(), id="legion_go_s", start_level=4, res=[1920, 1200])
    compat_root = os.path.join(home, ".local/share/Steam/steamapps/compatdata")
    for lib in libs:
        for m in sorted(glob.glob(os.path.join(lib, "appmanifest_*.acf"))):
            t = C.read_text(m)
            aid = re.search(r'"appid"\s+"(\d+)"', t)
            name = re.search(r'"name"\s+"([^"]+)"', t)
            idir = re.search(r'"installdir"\s+"([^"]+)"', t)
            if not (aid and idir) or re.search(r"proton|runtime|redistrib|steamworks|lossless",
                                                (name.group(1) if name else ""), re.I):
                continue
            appid = aid.group(1)
            install = os.path.join(lib, "common", idir.group(1))
            compat = os.path.join(lib, "compatdata", appid)        # префікс поруч із грою
            if not os.path.isdir(compat):
                compat = os.path.join(compat_root, appid)
            if not os.path.isdir(compat):
                print(f"\n## {name.group(1) if name else appid} ({appid}) — ще не запускалась (немає префікса)")
                continue
            r = audit(appid, install, compat, device)
            print(f"\n## {name.group(1) if name else appid} ({appid}) — {r.get('engine')}")
            if r.get("config"):
                print(f"   файл: {r['config'].split('steamuser/')[-1].split('common/')[-1]}")
            if r.get("sav"):
                import gvas
                vals = {k: v for k, v in gvas.load(r["sav"]).items()
                        if G._norm(G.leaf_name(k)) in G.UE_GROUPS or G._norm(G.leaf_name(k)) + "quality" in G.UE_GROUPS}
                print(f"   + сейв: {os.path.basename(r['sav'])} — синхронізується: {vals}")
            if r.get("overlays"):
                print("   + накладки (гра читає після GameUserSettings): " + ", ".join(os.path.basename(p) for p in r["overlays"]))
            if r.get("note"):
                print(f"   {r['note']}")
            if r.get("steps"):
                print(f"   керує ({len(r['steps'])}): " + "; ".join(f"{k} {'/'.join(v)}" for k, v in r["steps"].items()))
            if r.get("max_at_7"):
                print(f"   ⚠ на сходинці 7 досі як на 1-й: {', '.join(sorted(r['max_at_7']))}")
            if r.get("unmanaged"):
                print(f"   ⚠ НЕ керує: {', '.join(r['unmanaged'])}")


if __name__ == "__main__":
    main()
