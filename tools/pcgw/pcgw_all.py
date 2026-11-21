"""Уся база PCGamingWiki: для кожної сторінки з Category:Games — Steam AppID і шляхи налаштувань.

Вихід: pcgw_all.jsonl — по рядку на сторінку {"page", "appids": [...], "config": [...], "engine": [...]}.
Відновлюється з місця зупинки (pcgw_all.state). Пауза між запитами — щоб не навантажувати вікі.
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
UA = {"User-Agent": "AutoFPS-config-research/1.2 (Decky plugin; github.com/olliolliolli/deck-auto-fps)"}
API = "https://www.pcgamingwiki.com/w/api.php?"


def req(params):
    url = API + urllib.parse.urlencode(params)
    for attempt in range(5):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90)
            return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print("retry", attempt, repr(e)[:120], flush=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("API недоступний")


def appids(wt):
    out = []
    for m in re.finditer(r"\|\s*steam appid(?:\s+side)?\s*=\s*([0-9 ,]+)", wt, re.I):
        out += [x for x in re.split(r"[ ,]+", m.group(1).strip()) if x.isdigit()]
    return list(dict.fromkeys(out))


def engines(wt):
    return list(dict.fromkeys(m.group(1).strip() for m in
                              re.finditer(r"\{\{Infobox game/row/engine\|([^|}]+)", wt)))


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
                rec = {"page": pg["title"], "appids": appids(wt), "engine": engines(wt),
                       "config": [c for c in templates(wt, "config")
                                  if c["platform"].lower() in ("windows", "steam")]}
                if rec["appids"] or rec["config"]:
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
