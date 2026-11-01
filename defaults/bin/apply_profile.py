#!/usr/bin/env python3
"""
Ручні профілі графіки (мають пріоритет над автопідбором).

Профіль: ~/.config/autofps/profiles/<appid>.json  (приклад — profiles/EXAMPLE_unreal.json)
Рівень береться з ~/.config/autofps/games.json (останній підібраний базовий FPS).
Ключ пресету — найбільший ключ <= базового FPS.
Формати: "ini" (з секціями) та "kv" (key=value без секцій).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofps_common as C  # noqa: E402
from autotune import backup_once, patch_ini  # noqa: E402


def expand(path, appid):
    compat = os.environ.get("STEAM_COMPAT_DATA_PATH") or os.path.join(
        C.HOME, ".local/share/Steam/steamapps/compatdata", appid)
    return os.path.expanduser(
        path.replace("{compatdata}", compat).replace("{home}", C.HOME)
            .replace("{appid}", appid))


def pick_tier(tiers, base):
    keys = sorted(int(k) for k in tiers)
    chosen = [k for k in keys if k <= base]
    return str(chosen[-1] if chosen else keys[0])


def main_for(appid):
    profile = C.load_json(os.path.join(C.PROFILES_DIR, f"{appid}.json"), None)
    game = C.load_json(C.GAMES_FILE, {}).get(appid)
    if not profile or not game:
        return
    base = int(game.get("base", 60))
    for entry in profile.get("files", []):
        path = expand(entry["path"], appid)
        if not os.path.isfile(path):
            print(f"[{time.ctime()}] {appid}: немає файлу {path}")
            continue
        tier = pick_tier(entry["tiers"], base)
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        backup_once(path)
        new = patch_ini(lines, entry["tiers"][tier], entry.get("type", "ini") == "ini")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(new)
        print(f"[{time.ctime()}] {appid}: ручний пресет '{tier}' -> {path}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main_for(sys.argv[1])
