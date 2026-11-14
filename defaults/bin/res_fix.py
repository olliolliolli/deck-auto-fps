#!/usr/bin/env python3
"""AutoFPS · роздільність, збережена в САМІЙ грі, — під поточний екран.

Гра, запущена на телевізорі, часто зберігає у власних налаштуваннях 16:9 (напр. 1920×1080),
і потім на екрані консолі (16:10) йде з чорними смугами — і навпаки. Перед запуском гри
(через журнал змін, тож відкочується при падінні) виправляємо пари «ширина/висота» у:
  * реєстрі Proton (pfx/user.reg): FullscreenWidth/Height, ResolutionWidth/Height, ScreenWidth/Height…
  * Unreal: GameUserSettings.ini — ResolutionSizeX/Y, LastUserConfirmedResolutionSizeX/Y,
    DesiredScreenWidth/Height
  * портах Feral: preferences — FullscreenWidth/Height, WindowWidth/Height…
Змінюємо лише значення, у яких співвідношення сторін не збігається з екраном (> 3%).

CLI:  python3 res_fix.py <appid> <команда гри…>          — перед грою: під поточний екран
      python3 res_fix.py restore <appid> <команда гри…>  — після гри: назад під екран консолі
Так гра завжди зберігає 16:10, а на телевізорі отримує 16:9 лише на час гри.
"""
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofps_common as C  # noqa: E402

PAIRS = [("FullscreenWidth", "FullscreenHeight"), ("ResolutionWidth", "ResolutionHeight"),
         ("ScreenWidth", "ScreenHeight"), ("WindowWidth", "WindowHeight"),
         ("UserWindowWidth", "UserWindowHeight"), ("ResolutionSizeX", "ResolutionSizeY"),
         ("LastUserConfirmedResolutionSizeX", "LastUserConfirmedResolutionSizeY"),
         ("DesiredScreenWidth", "DesiredScreenHeight")]
STD_16_9 = [(1280, 720), (1600, 900), (1920, 1080), (2560, 1440), (3840, 2160)]
STD_16_10 = [(1280, 800), (1440, 900), (1680, 1050), (1920, 1200), (2560, 1600)]


def panel_size(drm=None):
    drm = drm or C.DRM
    for p in sorted(glob.glob(os.path.join(drm, "card*-eDP-*", "modes"))):
        try:
            with open(p, encoding="utf-8") as f:
                m = re.match(r"^(\d+)x(\d+)", f.readline().strip())
            if m:
                return int(m.group(1)), int(m.group(2))
        except OSError:
            pass
    return None


def screen(drm=None):
    """(ширина, висота) екрана, на якому зараз буде гра."""
    ext = C.external_display(drm)
    if ext and ext.get("w"):
        return ext["w"], ext["h"]
    return panel_size(drm)


def target_for(w, h, scr, appid=None, res_file=None):
    """Нова роздільність для збереженої (w, h) або None, якщо співвідношення вже правильне."""
    if not (w and h and scr):
        return None
    sw, sh = scr
    if abs((w / h) - (sw / sh)) / (sw / sh) <= 0.03:
        return None
    want = sw / sh
    manual = C.load_json(res_file, {}).get(str(appid)) if res_file and appid else None
    if manual and re.match(r"^\d+x\d+$", manual):
        mw, mh = (int(x) for x in manual.split("x"))
        if C.external_display() and abs(want - 16 / 9) < 0.05:
            mw, mh = (int(x) for x in C.dock_resolution(manual, sh).split("x"))
        if abs(mw / mh - want) / want <= 0.03:
            return mw, mh
    std = STD_16_9 if abs(want - 16 / 9) < 0.05 else STD_16_10 if abs(want - 1.6) < 0.05 else None
    if std:
        # та сама «величина» картинки: найближча стандартна за шириною, не більша за екран
        fit = [r for r in std if r[0] <= sw and r[1] <= sh] or [std[0]]
        return min(fit, key=lambda r: abs(r[0] - w))
    nh = int(round(w / want / 2)) * 2
    return (w, nh) if nh <= sh else (sw, sh)


# ------------------------------------------------------------------ формати

def _fix_lines(lines, val_re, fmt, scr, appid, res_file, log, where):
    """Знайти пари ширина/висота в межах одного блоку (секції) і виправити."""
    found = {}
    for i, line in enumerate(lines):
        m = val_re.match(line)
        if m:
            found[m.group("k")] = (i, m)
    changed = False
    for kw, kh in PAIRS:
        if kw in found and kh in found:
            (iw, mw), (ih, mh) = found[kw], found[kh]
            try:
                w, h = fmt["get"](mw), fmt["get"](mh)
            except ValueError:
                continue
            t = target_for(w, h, scr, appid, res_file)
            if t:
                lines[iw] = fmt["set"](mw, t[0])
                lines[ih] = fmt["set"](mh, t[1])
                log(f"роздільність у грі: {where} {kw}/{kh} {w}×{h} → {t[0]}×{t[1]}")
                changed = True
    return changed


def _sections(lines, is_header):
    start = 0
    for i, line in enumerate(lines):
        if is_header(line) and i > start:
            yield start, i
            start = i
    yield start, len(lines)


def fix_reg(path, scr, appid=None, res_file=None, log=print):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        lines = f.readlines()
    val = re.compile(r'^"(?P<k>[A-Za-z]+)"=dword:(?P<v>[0-9a-fA-F]{8})(?P<e>\s*)$')
    fmt = {"get": lambda m: int(m.group("v"), 16),
           "set": lambda m, n: f'"{m.group("k")}"=dword:{n:08x}{m.group("e")}'}
    changed = False
    for a, b in _sections(lines, lambda s: s.startswith("[")):
        head = lines[a]
        if not head.startswith("[Software\\\\") or re.match(r"^\[Software\\\\(Wine|Microsoft|Valve)", head):
            continue
        part = lines[a:b]
        if _fix_lines(part, val, fmt, scr, appid, res_file, log, "реєстр"):
            lines[a:b] = part
            changed = True
    if changed:
        with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.writelines(lines)
    return changed


def fix_ini(path, scr, appid=None, res_file=None, log=print):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        lines = f.readlines()
    val = re.compile(r"^(?P<k>[A-Za-z]+)=(?P<v>\d+)(?P<e>\s*)$")
    fmt = {"get": lambda m: int(m.group("v")),
           "set": lambda m, n: f"{m.group('k')}={n}{m.group('e')}"}
    changed = False
    for a, b in _sections(lines, lambda s: s.startswith("[")):
        part = lines[a:b]
        if _fix_lines(part, val, fmt, scr, appid, res_file, log, os.path.basename(path)):
            lines[a:b] = part
            changed = True
    if changed:
        with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.writelines(lines)
    return changed


def fix_feral(path, scr, appid=None, res_file=None, log=print):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        lines = f.readlines()
    val = re.compile(r'^(?P<p>\s*<value name=")(?P<k>[A-Za-z]+)(?P<q>" type="integer">)(?P<v>\d+)(?P<e></value>\s*)$')
    fmt = {"get": lambda m: int(m.group("v")),
           "set": lambda m, n: f"{m.group('p')}{m.group('k')}{m.group('q')}{n}{m.group('e')}"}
    changed = False
    for a, b in _sections(lines, lambda s: "<key " in s):
        part = lines[a:b]
        if _fix_lines(part, val, fmt, scr, appid, res_file, log, "Feral"):
            lines[a:b] = part
            changed = True
    if changed:
        with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.writelines(lines)
    return changed


def run(appid, args, drm=None, compat=None, log=print, restore=False):
    """restore=True — після виходу з гри: роздільність у грі назад під екран консолі (16:10)."""
    if C.load_json(C.SETTINGS_FILE, {}).get("dock_auto", True) is False:
        return 0
    scr = panel_size(drm) if restore else screen(drm)
    if not scr or str(appid) in ("", "0"):
        return 0
    res_file = os.path.join(C.CFG_DIR, "resolution.json")
    compat = compat or os.environ.get("STEAM_COMPAT_DATA_PATH") or \
        os.path.join(C.HOME, ".local/share/Steam/steamapps/compatdata", str(appid))
    n = 0
    reg = os.path.join(compat, "pfx", "user.reg")
    if os.path.isfile(reg):
        n += fix_reg(reg, scr, appid, res_file, log)
        user = os.path.join(compat, "pfx", "drive_c", "users", "steamuser")
        for ini in glob.glob(os.path.join(user, "AppData", "Local", "*", "Saved", "Config", "*",
                                          "GameUserSettings.ini")):
            n += fix_ini(ini, scr, appid, res_file, log)
    install = None
    for a in args:
        m = re.search(r"(.*/steamapps/common/[^/]+)", a)
        if m:
            install = m.group(1)
            break
    try:
        import generic_settings as G
        for _fmt, p in G.native_candidates(install):
            n += fix_feral(p, scr, appid, res_file, log)
    except Exception as e:  # noqa: BLE001
        log(f"роздільність у грі (Feral): {e!r}")
    return n


if __name__ == "__main__":
    if len(sys.argv) > 1:
        try:
            if sys.argv[1] == "restore":
                run(sys.argv[2], sys.argv[3:], restore=True)
            else:
                run(sys.argv[1], sys.argv[2:])
        except Exception as e:  # noqa: BLE001 — гру запускаємо за будь-яких умов
            print(f"res_fix error: {e!r}")
