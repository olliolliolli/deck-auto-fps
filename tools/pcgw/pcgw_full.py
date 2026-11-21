"""Повний збір фактів PCGamingWiki для AutoFPS (усі сторінки Category:Games).

На кожну сторінку — рядок JSON:
  page, appids, engine, config (Windows/Steam шляхи), api (dx/vulkan/opengl версії),
  upscaling, framegen, anticheat, readonly (вікі радить «лише для читання» для файлу налаштувань),
  systemsettings (рядки cvar=значення з порад для Engine.ini), notes (короткі фрагменти фікс-боксів
  про налаштування/конфіг).
Відновлюється з місця зупинки.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pcgw_fetch import templates  # noqa: E402

OUT = sys.argv[1]
STATE = OUT + ".state"
UA = {"User-Agent": "AutoFPS-config-research/1.3 (Decky plugin; github.com/olliolliolli/deck-auto-fps)"}
API = "https://www.pcgamingwiki.com/w/api.php?"


def req(params):
    url = API + urllib.parse.urlencode(params)
    for attempt in range(6):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90)
            return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print("retry", attempt, repr(e)[:120], flush=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("API недоступний")


def field(wt, name):
    m = re.search(r"\|\s*" + re.escape(name) + r"\s*=\s*([^\n|]*)", wt, re.I)
    return m.group(1).strip() if m else ""


def appids(wt):
    out = []
    for m in re.finditer(r"\|\s*steam appid(?:\s+side)?\s*=\s*([0-9 ,]+)", wt, re.I):
        out += [x for x in re.split(r"[ ,]+", m.group(1).strip()) if x.isdigit()]
    return list(dict.fromkeys(out))


def clean(t):
    t = re.sub(r"<ref.*?(</ref>|/>)", "", t, flags=re.S | re.I)
    t = re.sub(r"\{\{(?:ii|\+\+|--|mm)\}\}", "", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def facts(wt, title=""):
    rec = {
        "engine": list(dict.fromkeys(m.group(1).strip() for m in
                                     re.finditer(r"\{\{Infobox game/row/engine\|([^|}]+)", wt))),
        "config": [c for c in templates(wt, "config") if c["platform"].lower() in ("windows", "steam")],
        "api": {k: field(wt, k) for k in ("direct3d versions", "vulkan versions", "opengl versions",
                                           "windows 64-bit exe") if field(wt, k)},
        "upscaling": field(wt, "upscaling tech"),
        "framegen": field(wt, "framegen tech"),
        "anticheat": field(wt, "anticheat"),
    }
    low = wt.lower().replace(title.lower(), " ") if title else wt.lower()
    rec["readonly"] = bool(re.search(r"read[- ]only", low)) and bool(
        re.search(r"(\.ini|\.cfg|\.json|\.xml|config|settings)[^\n]{0,400}read[- ]only|read[- ]only[^\n]{0,400}"
                  r"(\.ini|\.cfg|\.json|\.xml|config)", low))
    ss = []
    for m in re.finditer(r"(?:<code>|^\s*)([A-Za-z]+\.[A-Za-z0-9_.]+)\s*=\s*([-0-9.A-Za-z]+)\s*(?:</code>)?", wt, re.M):
        k = m.group(1)
        if re.match(r"(r|sg|fx|foliage|grass|t|a|D3D12|niagara|Slate|s|p)\.", k):
            ss.append(f"{k}={m.group(2)}")
    rec["systemsettings"] = list(dict.fromkeys(ss))[:80]
    notes = []
    for m in re.finditer(r"\{\{Fixbox\|description=([^|]+)\|", wt):
        notes.append(clean(m.group(1))[:160])
    rec["notes"] = notes[:30]
    return rec


def main():
    cont = json.load(open(STATE)) if os.path.isfile(STATE) else {}
    n = 0
    with open(OUT, "a", encoding="utf-8") as out:
        while True:
            p = {"action": "query", "format": "json", "formatversion": "2", "generator": "categorymembers",
                 "gcmtitle": "Category:Games", "gcmlimit": "50", "gcmnamespace": "0",
                 "prop": "revisions", "rvprop": "content", "rvslots": "main"}
            p.update(cont)
            d = req(p)
            for pg in d.get("query", {}).get("pages", []):
                revs = pg.get("revisions") or []
                wt = revs[0]["slots"]["main"]["content"] if revs else ""
                rec = {"page": pg["title"], "appids": appids(wt)}
                rec.update(facts(wt, pg["title"]))
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
            out.flush()
            if "continue" not in d:
                break
            cont = d["continue"]
            json.dump(cont, open(STATE, "w"))
            if n % 1000 < 50:
                print(f"{n} сторінок", flush=True)
            time.sleep(1.0)
    print("done", n, flush=True)


if __name__ == "__main__":
    main()
