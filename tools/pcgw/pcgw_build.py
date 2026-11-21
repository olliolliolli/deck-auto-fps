"""pcgw_all.jsonl → defaults/bin/config_paths.json (компактно, лише ігри зі Steam AppID і шляхами)."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pcgw_convert import convert  # noqa: E402

SRC, OUT = sys.argv[1], sys.argv[2]


def main():
    db, pages = {}, 0
    with open(SRC, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            pages += 1
            paths = []
            for c in rec.get("config", []):
                for part in re.split(r"<br\s*/?>", c["path"]):
                    v = convert(part)
                    if v and v not in paths and v.startswith(("pfx:", "game:", "reg:")):
                        paths.append(v)
            if not rec.get("appids") or not paths:
                continue
            for a in rec["appids"]:
                e = db.setdefault(a, {"paths": []})
                for p in paths:
                    if p not in e["paths"]:
                        e["paths"].append(p)
                if rec.get("engine"):
                    e["engine"] = rec["engine"][0]
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    print(f"сторінок {pages}, ігор Steam із шляхами {len(db)}, розмір {os.path.getsize(OUT) // 1024} КБ")


if __name__ == "__main__":
    main()
