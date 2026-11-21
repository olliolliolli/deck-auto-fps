"""pcgw.json → config_paths.json для плагіна.

Шляхи PCGamingWiki (Windows) переводяться у відносні шаблони:
  "pfx:AppData/Local/CD Projekt Red/Cyberpunk 2077"   — від steamuser у префіксі Proton
  "game:engine/config"                                — від теки гри
  "reg:Software/Crytek/Hunt"                          — HKCU у user.reg (для довідки)
Плейсхолдери {{p|uid}}, {{p|username}} … → «*».
"""
import json
import re
import sys

SRC, OUT = (sys.argv[1:3] + [None, None])[:2]

ROOTS = [
    (r"userprofile\\appdata\\locallow", "pfx:AppData/LocalLow"),
    (r"userprofile\\appdata\\local", "pfx:AppData/Local"),
    (r"userprofile\\appdata\\roaming", "pfx:AppData/Roaming"),
    (r"localappdata", "pfx:AppData/Local"),
    (r"appdata", "pfx:AppData/Roaming"),
    (r"userprofile\\documents", "pfx:Documents"),
    (r"userprofile\\saved games", "pfx:Saved Games"),
    (r"userprofile", "pfx:"),
    (r"public", "pub:"),
    (r"programdata", "pd:"),
    (r"game", "game:"),
    (r"hkcu", "reg:"),
    (r"hklm", "hklm:"),
    (r"steam", "steam:"),
]
WILD = {"uid", "username", "userid", "user id", "steamid", "steam id", "id", "user"}


def convert(path):
    p = path.strip()
    p = re.sub(r"<ref.*?(</ref>|/>)", "", p, flags=re.S | re.I)
    p = re.sub(r"\{\{note\|.*?\}\}", "", p, flags=re.I)
    m = re.match(r"\{\{\s*p\s*\|\s*([^}]+?)\s*\}\}(.*)$", p, re.I | re.S)
    if not m:
        return None
    root, rest = m.group(1).strip().lower(), m.group(2)
    prefix = None
    for pat, val in ROOTS:
        if re.fullmatch(pat, root):
            prefix = val
            break
    if prefix is None:
        return None

    def wild(mm):
        name = mm.group(1).strip().lower()
        return "*" if name in WILD or "id" in name else "*"
    rest = re.sub(r"\{\{\s*p\s*\|\s*([^}]+?)\s*\}\}", wild, rest, flags=re.I)
    rest = re.sub(r"\{\{[^}]*\}\}", "*", rest)
    rest = re.sub(r"<[^>]+>", "", rest)
    rest = rest.replace("\\", "/").strip().strip("/")
    rest = re.sub(r"/+", "/", rest)
    rest = re.sub(r"\s*\(.*?\)\s*$", "", rest).strip()
    if any(c in rest for c in "|[]{}"):
        return None
    rest = rest.replace("''", "").replace("&#39;", "'")
    rest = re.sub(r"^Lari'an", "Larian", rest)
    if prefix.endswith(":"):
        return prefix + rest
    return prefix + ("/" + rest if rest else "")


def main():
    src = json.load(open(SRC, encoding="utf-8"))
    out = {}
    for appid, e in src.items():
        paths = []
        for c in e.get("config", []):
            if c.get("platform", "").lower() not in ("windows", "steam"):
                continue
            for part in re.split(r"<br\s*/?>", c["path"]):
                v = convert(part)
                if v and v not in paths and not v.startswith(("steam:", "hklm:", "pd:", "pub:")):
                    paths.append(v)
        if paths:
            out[appid] = {"name": e.get("name", ""), "paths": paths, "page": e.get("page")}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
    print(f"{len(out)} ігор із шляхами налаштувань з {len(src)}")


if __name__ == "__main__":
    main()
