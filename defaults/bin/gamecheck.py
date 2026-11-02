#!/usr/bin/env python3
"""Чи оновилась гра з минулого запуску (buildid у steamapps/appmanifest_<appid>.acf).

Після оновлення: позначає графіку для повторного заміру і лишає записку для плагіна
(~/.cache/autofps/game_updated.json) — плагін покаже, що все перевірено й відновлено.
FSR 4 / OptiScaler і твіки рушіїв обгортка перевіряє на кожному запуску сама.

  gamecheck.py <appid> [args гри…]
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofps_common as C  # noqa: E402

BUILDS_FILE = os.path.join(C.CFG_DIR, "builds.json")


def find_install(args):
    env = os.environ.get("STEAM_COMPAT_INSTALL_PATH")
    if env and os.path.isdir(env):
        return env
    for a in args:
        m = re.search(r"(.*/steamapps/common/[^/]+)", a)
        if m and os.path.isdir(m.group(1)):
            return m.group(1)
    return None


def manifest(appid, install):
    cands = []
    if install:
        cands.append(os.path.join(os.path.dirname(os.path.dirname(install)), f"appmanifest_{appid}.acf"))
    cands.append(os.path.join(C.HOME, ".local/share/Steam/steamapps", f"appmanifest_{appid}.acf"))
    for p in cands:
        if os.path.isfile(p):
            return p
    return None


def read_build(path):
    text = C.read_text(path)
    m = re.search(r'"buildid"\s+"(\d+)"', text)
    upd = re.search(r'"LastUpdated"\s+"(\d+)"', text)
    return (m.group(1) if m else None), (int(upd.group(1)) if upd else 0)


def check(appid, args, log=print):
    install = find_install(args)
    mf = manifest(appid, install)
    if not mf:
        return None
    build, updated = read_build(mf)
    if not build:
        return None
    builds = C.load_json(BUILDS_FILE, {})
    old = builds.get(appid)
    builds[appid] = build
    C.save_json(BUILDS_FILE, builds)
    if not old or old == build:
        return None
    log(f"гру оновлено: збірка {old} → {build}")
    # графіку перевіряємо заново на поточній сходинці (оновлення могло змінити продуктивність)
    tune = C.load_json(C.TUNE_FILE, {})
    touched = 0
    for key, e in tune.items():
        if key.split(":")[0] == appid and isinstance(e, dict):
            for k in ("converged", "ineffective", "last"):
                e.pop(k, None)
            e["note"] = "Гру оновлено — перевіряю графіку заново"
            touched += 1
    if touched:
        C.save_json(C.TUNE_FILE, tune)
    note = {"appid": appid, "old": old, "new": build, "t": int(time.time()),
            "manifest_updated": updated, "retune": bool(touched)}
    os.makedirs(C.CACHE_DIR, exist_ok=True)
    with open(os.path.join(C.CACHE_DIR, "game_updated.json"), "w", encoding="utf-8") as f:
        json.dump(note, f)
    return note


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        check(sys.argv[1], sys.argv[2:])
