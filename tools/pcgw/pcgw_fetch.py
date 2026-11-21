"""Шляхи до файлів налаштувань з PCGamingWiki для всієї бібліотеки Steam.

Вхід: library.json — [[appid, name], ...]. Вихід: pcgw.json — {appid: {page, config: [...], saves: [...]}}.
Кешує вже отримане (можна перезапускати). Пауза між запитами — щоб не навантажувати вікі.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

LIB, OUT = (sys.argv[1:3] + [None, None])[:2]
UA = {"User-Agent": "AutoFPS-config-research/1.0 (Decky plugin; contact via GitHub olliolliolli/deck-auto-fps)"}
DELAY = 1.0


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


def page_for(appid):
    req = urllib.request.Request(f"https://www.pcgamingwiki.com/api/appid.php?appid={appid}", headers=UA, method="HEAD")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoRedirect)
    try:
        op.open(req, timeout=30)
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "")
        if "/wiki/" in loc:
            return urllib.parse.unquote(loc.split("/wiki/", 1)[1])
    return None


def templates(text, kind):
    out = []
    for m in re.finditer(r"\{\{Game data/" + kind + r"\|([^|}]*)\|", text):
        plat = m.group(1).strip()
        # аргументи до закриваючих }} з урахуванням вкладених {{p|...}}
        i, depth, start = m.end(), 2, m.end()
        args, cur = [], start
        while i < len(text) and depth > 0:
            if text.startswith("{{", i):
                depth += 2
                i += 2
                continue
            if text.startswith("}}", i):
                depth -= 2
                if depth == 0:
                    args.append(text[cur:i])
                i += 2
                continue
            if text[i] == "|" and depth == 2:
                args.append(text[cur:i])
                cur = i + 1
            i += 1
        for a in args:
            a = a.strip()
            if a:
                out.append({"platform": plat, "path": a})
    return out


def main():
    lib = json.load(open(LIB, encoding="utf-8"))
    res = json.load(open(OUT, encoding="utf-8")) if os.path.isfile(OUT) else {}
    for n, (appid, name) in enumerate(lib):
        a = str(appid)
        if a in res:
            continue
        try:
            page = page_for(a)
            time.sleep(DELAY)
            entry = {"name": name, "page": page, "config": [], "saves": []}
            if page:
                url = ("https://www.pcgamingwiki.com/w/api.php?action=parse&prop=wikitext&format=json&page="
                       + urllib.parse.quote(page))
                _, body = get(url)
                wt = json.loads(body).get("parse", {}).get("wikitext", {}).get("*", "")
                entry["config"] = templates(wt, "config")
                entry["saves"] = templates(wt, "saves")
                time.sleep(DELAY)
            res[a] = entry
        except Exception as e:  # noqa: BLE001
            res[a] = {"name": name, "error": repr(e)[:200]}
            time.sleep(5)
        if n % 20 == 0:
            json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            print(f"{n}/{len(lib)}", flush=True)
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("done", len(res))


if __name__ == "__main__":
    main()
