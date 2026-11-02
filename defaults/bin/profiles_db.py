#!/usr/bin/env python3
"""База готових профілів AutoFPS (GitHub: profiles/<пристрій>/<appid>.json).

Профіль = підібрана графіка (сходинка драбини автопідбору) + рівень FPS + TDP + роздільність
для конкретного пристрою. Нова гра, для якої в базі є профіль, одразу запускається з ним —
без кількох запусків підбору. Свої вдалі профілі можна відправити в базу (кнопка в плагіні).

  profiles_db.py fetch <appid> [args гри…]   — перед запуском нової гри (обгортка)
  profiles_db.py export <appid>              — надрукувати профіль цієї гри (JSON)
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofps_common as C  # noqa: E402

REPO = "olliolliolli/deck-auto-fps"
RAW = f"https://raw.githubusercontent.com/{REPO}/main/profiles"
COMMUNITY_DIR = os.path.join(C.CFG_DIR, "community")
FORMAT = 1
TUNE_KEYS = ("level", "track", "target", "ladder_sig", "steps", "engine", "lowres", "summary")


def export(appid, device=None):
    """Профіль гри з того, що вже підібрано на цьому пристрої (None — ще не готовий)."""
    device = device or C.detect_device()
    appid = str(appid)
    e = C.load_json(C.TUNE_FILE, {}).get(f"{appid}:{device['id']}") or {}
    g = C.load_json(C.GAMES_FILE, {}).get(appid) or {}
    if not (e.get("converged") or e.get("ineffective")) and not g.get("base"):
        return None
    res = C.load_json(os.path.join(C.CFG_DIR, "resolution.json"), {}).get(appid)
    opti = C.load_json(os.path.join(C.CFG_DIR, "optiscaler_games", f"{appid}.json"), {})
    prof = {
        "format": FORMAT, "appid": appid, "device": device["id"],
        "name": opti.get("name") or e.get("name") or "",
        "created": int(time.time()),
        "graphics": {k: e[k] for k in TUNE_KEYS if k in e} if e.get("converged") else None,
        "graphics_values": e.get("applied_values") if e.get("converged") else None,
        "fps": {"base": g.get("base"), "mult": g.get("mult", 1)} if g.get("base") else None,
        "tdp": {k.split(":")[1] + ":" + k.split(":")[2]: v for k, v in (g.get("tdp") or {}).items()
                if k.startswith(device["id"] + ":")},
        "resolution": res,
        "upscaler": {"method": opti.get("method"), "label": opti.get("upscaler_label")} if opti.get("enabled") else None,
    }
    return prof


def share_url(prof):
    """Посилання «створити файл» на GitHub з уже заповненим профілем."""
    name = f"profiles/{prof['device']}/{prof['appid']}.json"
    body = json.dumps(prof, ensure_ascii=False, indent=2)
    return (f"https://github.com/{REPO}/new/main?filename={urllib.parse.quote(name)}"
            f"&value={urllib.parse.quote(body)}")


def fetch(appid, device_id, timeout=4):
    url = f"{RAW}/{device_id}/{appid}.json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — нема мережі/профілю — граємо як звичайно
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT or str(data.get("appid")) != str(appid):
        return None
    return data


def apply(prof, device, log=print):
    """Засіває стан AutoFPS з профілю (лише для гри, якої ще не підбирали)."""
    appid = str(prof["appid"])
    tune = C.load_json(C.TUNE_FILE, {})
    key = f"{appid}:{device['id']}"
    gr = prof.get("graphics")
    if gr and key not in tune:
        tune[key] = {**gr, "start_scaled": True, "converged": True, "lowres_done": True,
                     "from_community": True,
                     "note": "Готовий профіль з бази AutoFPS", "decision_note": "Готовий профіль з бази AutoFPS"}
        C.save_json(C.TUNE_FILE, tune)
    games = C.load_json(C.GAMES_FILE, {})
    if prof.get("fps") and appid not in games:
        games[appid] = {"base": int(prof["fps"]["base"]), "mult": int(prof["fps"].get("mult") or 1),
                        "native_p10": 0, "device": device["id"], "updated": int(time.time()),
                        "from_community": True,
                        "tdp": {f"{device['id']}:{k}": v for k, v in (prof.get("tdp") or {}).items()}}
        C.save_json(C.GAMES_FILE, games)
    if prof.get("resolution"):
        rp = os.path.join(C.CFG_DIR, "resolution.json")
        r = C.load_json(rp, {})
        r.setdefault(appid, prof["resolution"])
        C.save_json(rp, r)
    os.makedirs(COMMUNITY_DIR, exist_ok=True)
    C.save_json(os.path.join(COMMUNITY_DIR, f"{appid}.json"), prof)
    note = {"appid": appid, "t": int(time.time()), "name": prof.get("name", ""),
            "summary": (gr or {}).get("summary", ""), "fps": prof.get("fps")}
    os.makedirs(C.CACHE_DIR, exist_ok=True)
    with open(os.path.join(C.CACHE_DIR, "community_applied.json"), "w", encoding="utf-8") as f:
        json.dump(note, f, ensure_ascii=False)
    log(f"профіль з бази AutoFPS: {prof.get('name') or appid} — {(gr or {}).get('summary', '')}")


def fetch_and_apply(appid, log=print):
    settings = C.load_json(C.SETTINGS_FILE, {})
    if not settings.get("community_profiles", True):
        return
    device = C.detect_device()
    appid = str(appid)
    if f"{appid}:{device['id']}" in C.load_json(C.TUNE_FILE, {}) or appid in C.load_json(C.GAMES_FILE, {}):
        return                                   # гру вже підбирали — свої дані важливіші
    if os.path.isfile(os.path.join(C.PROFILES_DIR, f"{appid}.json")):
        return                                   # ручний профіль
    prof = fetch(appid, device["id"])
    if prof:
        apply(prof, device, log)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[2].isdigit():
        if sys.argv[1] == "fetch":
            fetch_and_apply(sys.argv[2])
        elif sys.argv[1] == "export":
            print(json.dumps(export(sys.argv[2]), ensure_ascii=False, indent=2))
