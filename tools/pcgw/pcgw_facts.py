"""pcgw_full.jsonl → defaults/bin/game_facts.json: {appid: {e, dx, vk, up, fg, ac, ro}} (лише непорожнє)."""
import json
import re
import sys

SRC, OUT = sys.argv[1], sys.argv[2]
NO = {"", "false", "unknown", "none", "n/a", "no"}


def main():
    db = {}
    with open(SRC, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if not r.get("appids"):
                continue
            rec = {}
            if r.get("engine"):
                rec["e"] = r["engine"][0]
            api = r.get("api", {})
            dx = re.findall(r"\d+(?:\.\d+)?", api.get("direct3d versions", ""))
            if dx:
                rec["dx"] = sorted({d.split(".")[0] for d in dx}, key=int)
            if api.get("vulkan versions", "").strip().lower() not in NO:
                rec["vk"] = 1
            if r.get("upscaling", "").strip().lower() not in NO:
                rec["up"] = r["upscaling"][:80]
            if r.get("framegen", "").strip().lower() not in NO:
                rec["fg"] = r["framegen"][:60]
            ac = r.get("anticheat", "").strip()
            if ac.lower() not in NO:
                rec["ac"] = re.sub(r"\[\[|\]\]|\{\{[^}]*\}\}", "", ac)[:60]
            if r.get("readonly"):
                rec["ro"] = 1
            if rec:
                for a in r["appids"]:
                    db.setdefault(a, {}).update(rec)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    print(len(db), "ігор")


if __name__ == "__main__":
    main()
