#!/usr/bin/env python3
"""Твік Engine.ini для ігор на Unreal Engine 5 (Steam Deck / Legion Go S).

Додає в [SystemSettings] блок команд, які знижують підвисання (кеш шейдерів/PSO) і трохи
полегшують найдорожчі ефекти UE5 без помітного погіршення картинки на екрані портативки.
Блок позначено маркерами AutoFPS — його легко прибрати (вимкнути твік або «Повернути оригінал»).
Рядки, які користувач уже має в Engine.ini, не перезаписуються.

Шлях: steamapps/compatdata/<appid>/pfx/drive_c/users/steamuser/AppData/Local/<Проєкт>/Saved/Config/
       Windows|WinGDK/Engine.ini (поруч з GameUserSettings.ini)

  ue_tweaks.py apply  <appid> [args гри…]   — додати/оновити (або прибрати, якщо твік вимкнено)
  ue_tweaks.py remove <appid>               — прибрати
"""
import glob
import mmap
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofps_common as C  # noqa: E402
import autotune as T  # noqa: E402

BEGIN = "; >>> AutoFPS UE5 tweak (remove this block or turn the tweak off in AutoFPS) >>>"
END = "; <<< AutoFPS UE5 tweak <<<"
STATE_FILE = os.path.join(C.CFG_DIR, "ue_tweaks.json")

# лише значення, які не дорожчі за «низькі» налаштування рушія — твік ніколи не підвищує навантаження
TWEAKS = [
    # підвисання: кеш і попередня компіляція шейдерів/PSO
    ("r.ShaderPipelineCache.Enabled", "1"),
    ("r.PSOPrecaching", "1"),
    ("r.CreateShadersOnLoad", "1"),
    ("D3D12.PSO.DiskCache", "1"),
    ("D3D12.PSO.DriverOptimizedDiskCache", "1"),
    # рівніший час кадру
    ("r.GTSyncType", "1"),
    ("r.OneFrameThreadLag", "1"),
    ("r.Streaming.LimitPoolSizeToVRAM", "1"),
    # дешеві ефекти, яких на 7–8" екрані майже не видно
    ("r.MotionBlurQuality", "0"),
    ("r.SceneColorFringeQuality", "0"),
    ("r.VolumetricFog.GridPixelSize", "16"),
    ("r.VolumetricFog.GridSizeZ", "64"),
    ("r.VolumetricFog.HistoryMissSupersampleCount", "2"),
    ("r.SkyAtmosphere.FastSkyLUT", "1"),
    ("r.SkyAtmosphere.AerialPerspectiveLUT.FastApplyOnOpaque", "1"),
    # UE5: Nanite і відбиття Lumen трохи грубші — найбільший виграш на APU
    ("r.Nanite.MaxPixelsPerEdge", "2"),
    ("r.Lumen.Reflections.DownsampleFactor", "2"),
]


def is_ue5(install):
    """UE5 — за рядком версії рушія в *-Shipping.exe (результат кешується)."""
    exes = glob.glob(os.path.join(install, "*", "Binaries", "Win64", "*Shipping*.exe")) + \
        glob.glob(os.path.join(install, "*", "Binaries", "WinGDK", "*Shipping*.exe"))
    if not exes:
        return False
    state = C.load_json(STATE_FILE, {})
    cache = state.setdefault("_ue5", {})
    for exe in exes:
        try:
            st = os.stat(exe)
        except OSError:
            continue
        key = f"{exe}:{st.st_size}:{int(st.st_mtime)}"
        if key in cache:
            if cache[key]:
                return True
            continue
        found = False
        try:
            with open(exe, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
                found = m.find(b"++UE5+") >= 0 or m.find("++UE5+".encode("utf-16-le")) >= 0
        except (OSError, ValueError):
            pass
        cache[key] = found
        C.save_json(STATE_FILE, state)
        if found:
            return True
    return False


def engine_ini(install, compat):
    """Engine.ini поруч з GameUserSettings.ini гри (тека з'являється після першого запуску)."""
    gus = T.UnrealAdapter(install, compat, {}).locate()
    return os.path.join(os.path.dirname(gus), "Engine.ini") if gus else None


def _strip_block(lines):
    out, skip = [], False
    for ln in lines:
        s = ln.strip()
        if s == BEGIN:
            skip = True
            continue
        if skip and s == END:
            skip = False
            continue
        if not skip:
            out.append(ln)
    return out


def _write(path, lines):
    tmp = path + ".autofps.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.writelines(lines)
    os.replace(tmp, path)


def remove_from(path):
    if not path or not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8", errors="ignore", newline="") as f:
        lines = f.readlines()
    new = _strip_block(lines)
    if new == lines:
        return False
    _write(path, new)
    return True


def apply_to(path):
    lines = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="ignore", newline="") as f:
            lines = f.readlines()
    nl = "\r\n" if any(ln.endswith("\r\n") for ln in lines) else "\n"
    lines = _strip_block(lines)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    own = {ln.split("=", 1)[0].strip().lower() for ln in lines if "=" in ln and not ln.lstrip().startswith(";")}
    add = [(k, v) for k, v in TWEAKS if k.lower() not in own]
    if not add:
        return 0
    block = [BEGIN + nl] + [f"{k}={v}{nl}" for k, v in add] + [END + nl]
    sec = next((i for i, ln in enumerate(lines) if ln.strip().lower() == "[systemsettings]"), None)
    if sec is None:
        if lines and lines[-1].strip():
            lines.append(nl)
        lines += ["[SystemSettings]" + nl] + block
    else:
        lines[sec + 1:sec + 1] = block
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _write(path, lines)
    return len(add)


def run(cmd, appid, args, log=print):
    settings = C.load_json(C.SETTINGS_FILE, {})
    state = C.load_json(STATE_FILE, {})
    entry = state.get(str(appid), {})
    install = T.find_install(args) or entry.get("install")
    if not install:
        return
    compat = T.find_compat(str(appid))
    path = engine_ini(install, compat) or entry.get("path")
    want = cmd == "apply" and settings.get("ue5_tweak", True) and not entry.get("restore_pending")
    if not want:
        if remove_from(path):
            log(f"UE5 твік: прибрано з {path}")
        if str(appid) in state:
            state[str(appid)].update(applied=False, restore_pending=False)
            C.save_json(STATE_FILE, state)
        return
    if not T.UnrealAdapter.detect(install) or not is_ue5(install):
        return
    state = C.load_json(STATE_FILE, {})       # is_ue5 міг оновити кеш
    if not path:
        state[str(appid)] = {"install": install, "applied": False,
                             "note": "Engine.ini з'явиться після першого запуску — твік додам наступного разу"}
        C.save_json(STATE_FILE, state)
        log("UE5 твік: тека налаштувань гри ще не створена — наступного запуску")
        return
    n = apply_to(path)
    state[str(appid)] = {"install": install, "path": path, "applied": True, "lines": n, "t": int(time.time())}
    C.save_json(STATE_FILE, state)
    log(f"UE5 твік: {n} параметрів у {path}")


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("apply", "remove"):
        print(__doc__)
        return 1
    appid = sys.argv[2]
    if not re.match(r"^\d+$", appid):
        return 0
    run(sys.argv[1], appid, sys.argv[3:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
