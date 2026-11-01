#!/usr/bin/env python3
"""
AutoFPS · OptiScaler — «DLSS» для AMD одним натисканням.

Справжній DLSS працює лише на відеокартах NVIDIA. OptiScaler перехоплює DLSS у грі
й підставляє FSR 3.1 / XeSS / FSR 4, а FakeNVAPI робить так, що гра показує пункт DLSS.

Що взято з Decky Framegen і покращено:
  * ті самі файли й назви підмінної бібліотеки (dxgi.dll, winmm.dll, version.dll …),
    бекапи *.b оригінальних DLL гри і повне відновлення при вимкненні;
  * пошук теки гри: Unreal → <Project>/Binaries/Win64 (не Engine), виправлення
    для ігор з окремим лаунчером (Cyberpunk, Witcher 3, BG3, Hitman …);
  * виправлення ini для Proton (UseHQFont=false).
  Покращення:
  * OptiScaler завантажується й оновлюється з GitHub на пристрої (не вшитий у плагін);
  * профілі за рушієм (Unreal, RE Engine, Unity) і окремі профілі ігор;
  * відмова для ігор з античитом і для ігор без вбудованого DLSS, що потребують REFramework;
  * автовідновлення файлів після оновлення гри та автооновлення OptiScaler в іграх;
  * узгодження з генерацією кадрів lsfg-vk (не вмикаються разом);
  * вибір FSR 4 / FSR 3.1 / XeSS за відеокартою.

CLI (для обгортки запуску гри):
  optiscaler.py ensure <appid> <аргументи %command%…>   → друкує DLL=<stem> FG=<0|1>
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofps_common as C  # noqa: E402

REPO = "optiscaler/OptiScaler"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
UPDATE_TTL = 6 * 3600

# Додаткові файли FSR 4 — ті самі, що в Decky Framegen, з тими самими sha256
_BE = "https://github.com/xXJSONDeruloXx/OptiScaler-Bleeding-Edge/releases/download"
ASSETS = {
    # FSR 4.0.2c INT8 — старий сумісний варіант для RDNA2/3
    "fsr40_int8": {"url": f"{_BE}/opti-9-2-a/amd_fidelityfx_upscaler_dx12.dll",
                   "file": "amd_fidelityfx_upscaler_dx12.dll",
                   "sha256": "c7720bc16bede334f59a1a32cd22edbcbbb159685ed5240e61350a5fb0bc8a94"},
    # FSR 4.1.1 офіційна підміна драйвера — RDNA3/4
    "amdxcffx64_official_411": {"url": f"{_BE}/fsr-4-1-1/amdxcffx64.dll", "file": "amdxcffx64.dll",
                                "sha256": "a2b136b6affd35a49b141a936be935f7d5ddc8d8f9b8c9afbe62ff9ddb2538a0"},
    # FSR 4.1.1 для RDNA2 (Steam Deck, Legion Go S на Z2 Go) — збірка Valve
    "amdxcffx64_valve_411": {"url": f"{_BE}/fsr-4-1-1-rdna2/amdxcffx64.dll", "file": "amdxcffx64.dll",
                             "sha256": "4e7dc37aebea3a90e3d3cc43e24cb2b54176b2535315f20dbe63b3b7cfc56b1e"},
    "amdxc64_rdna2": {"url": f"{_BE}/fsr-4-1-1-rdna2/amdxc64.dll", "file": "amdxc64.dll",
                      "sha256": "a0a0af61d475e30a70966b3459f3793df772faf8f26ae3261d10554ff592cbd5"},
    # OptiScaler 0.10.0-pre1: потрібен для RDNA2-шляху, поки стабільна версія < 0.10
    "optiscaler_pre10": {"url": f"{_BE}/fsr-4-1-1-rdna2/OptiScaler_0.10.0-pre1.20260622_135940.dll",
                         "file": "OptiScaler.dll",
                         "sha256": "b374b19081cc066365d0c6da4808d768e16469b0cbdfc478b6e95999947d5364"},
}

# Варіанти FSR 4 за архітектурою (як у Decky Framegen)
FSR4_VARIANTS = {
    "rdna2-valve-411": {"label": "FSR 4.1.1 · Valve RDNA2",
                        "extra": ["amdxcffx64_valve_411", "amdxc64_rdna2"],
                        "injector_pre10": True,
                        "ini": {"FSR.Fsr4ForceModel": "2",
                                "Plugins.LoadCustomAmdxc64OnRdna2": "true"}},
    "rdna34-official-411": {"label": "FSR 4.1.1 · підміна драйвера",
                            "extra": ["amdxcffx64_official_411"], "ini": {}},
    "rdna4-native": {"label": "FSR 4.1.1 · SDK", "extra": [], "ini": {}},
    "rdna23-int8-402": {"label": "FSR 4.0.2 · INT8 (сумісний)", "extra": [], "upscaler": "fsr40_int8",
                        "ini": {}},
}

PROXY_NAMES = ["dxgi.dll", "winmm.dll", "version.dll", "dbghelp.dll", "d3d12.dll",
               "wininet.dll", "winhttp.dll", "OptiScaler.asi"]
# оригінальні DLL гри, які ми підміняємо — зберігаємо як *.b
ORIGINAL_BACKUPS = PROXY_NAMES + [
    "d3dcompiler_47.dll", "amd_fidelityfx_dx12.dll", "amd_fidelityfx_framegeneration_dx12.dll",
    "amd_fidelityfx_upscaler_dx12.dll", "amd_fidelityfx_vk.dll", "amdxcffx64.dll", "amdxc64.dll",
    "libxess.dll", "libxess_dx11.dll", "libxess_fg.dll", "libxell.dll",
]
SKIP_EXT = {".txt", ".md", ".pdf", ".bat", ".sh", ".exe", ".reg", ".url", ".png", ".jpg"}
MARKER = "AUTOFPS_OPTISCALER.json"

BAD_EXE = ["crashreport", "easyanticheat", "eac", "beclient", "eosbootstrap", "benchmark",
           "uninstall", "setup", "launcher", "updater", "bootstrap", "_redist", "prereq",
           "unitycrashhandler", "vc_redist", "dxsetup"]

# Ігри, де виконуваний файл не в корені або запускається через лаунчер
EXE_FIXUPS = {
    "1091500": "bin/x64",                     # Cyberpunk 2077
    "292030": "bin/x64_dx12",                 # The Witcher 3
    "1086940": "bin",                         # Baldur's Gate 3
    "1659040": "Retail",                      # HITMAN World of Assassination
    "1361210": "binaries",                    # Warhammer 40K: Darktide
    "552500": "binaries_dx12",                # Warhammer: Vermintide 2
}

# Профілі за рушієм (вікі OptiScaler)
ENGINE_PROFILES = {
    "unreal": {"label": "Unreal Engine", "dll": "dxgi.dll", "ini": {}},
    "re_engine": {"label": "RE Engine (Capcom)", "dll": "dxgi.dll",
                  "ini": {"Spoofing.Dxgi": "false", "Menu.OverlayMenu": "false"}},
    "unity": {"label": "Unity", "dll": "dxgi.dll", "ini": {}},
    "generic": {"label": "інший рушій", "dll": "dxgi.dll", "ini": {}},
}

# Профілі конкретних ігор (поверх профілю рушія)
GAME_PROFILES = {
    # Capcom RE Engine без вбудованого DLSS — потрібні REFramework + PureDark UpscalerBasePlugin
    "2050650": {"unsupported": "RE4 (2023) не має вбудованого DLSS: потрібні REFramework "
                               "(гілка pd-upscaler) і UpscalerBasePlugin — це не автоматизується"},
    "883710": {"unsupported": "RE2 не має вбудованого DLSS: потрібні REFramework і UpscalerBasePlugin"},
    "952060": {"unsupported": "RE3 не має вбудованого DLSS: потрібні REFramework і UpscalerBasePlugin"},
    "418370": {"unsupported": "RE7 не має вбудованого DLSS: потрібні REFramework і UpscalerBasePlugin"},
    "1196590": {"unsupported": "RE Village не має вбудованого DLSS (і FSR 4 там дає зіпсовану "
                               "картинку): потрібні REFramework і UpscalerBasePlugin"},
    "601150": {"unsupported": "Devil May Cry 5 не має вбудованого DLSS: потрібні REFramework і "
                              "UpscalerBasePlugin"},
    # Capcom RE Engine з вбудованим DLSS
    "1446780": {"ini": {"Hooks.RestoreComputeSignature": "true"}},   # Monster Hunter Rise
    "2246340": {},                                                    # Monster Hunter Wilds
    "2054970": {},                                                    # Dragon's Dogma 2
}

ANTICHEAT_MARKERS = ["EasyAntiCheat", "EasyAntiCheat_EOS", "BattlEye", "BEService",
                     "start_protected_game.exe", "vgk.sys", "EAAntiCheat"]


# ------------------------------------------------------------------ шляхи й стан

def root_dir():
    return os.path.join(C.HOME, ".local", "share", "autofps", "optiscaler")


def state_file():
    return os.path.join(C.CFG_DIR, "optiscaler.json")


def games_dir():
    return os.path.join(C.CFG_DIR, "optiscaler_games")


def game_file(appid):
    return os.path.join(games_dir(), f"{appid}.json")


def load_state():
    return C.load_json(state_file(), {})


def save_state(st):
    C.save_json(state_file(), st)


def load_game(appid):
    return C.load_json(game_file(appid), {})


def save_game(appid, data):
    C.save_json(game_file(appid), data)


def version_dir(tag):
    return os.path.join(root_dir(), re.sub(r"[^A-Za-z0-9._-]", "_", tag))


def installed_tag():
    st = load_state()
    tag = st.get("installed_tag")
    if tag and os.path.isfile(os.path.join(version_dir(tag), "OptiScaler.dll")):
        return tag
    return None


# ------------------------------------------------------------------ зовнішні команди

def _env():
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)  # бібліотеки Decky ламають системні програми
    env.pop("LD_PRELOAD", None)
    return env


def _run(cmd, timeout=120):
    r = subprocess.run(cmd, env=_env(), capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def http_get(url, dest=None, timeout=60):
    """Завантаження через системний curl (у Decky-Python часто немає сертифікатів)."""
    curl = shutil.which("curl") or "/usr/bin/curl"
    cmd = [curl, "-fsSL", "--retry", "2", "-A", "AutoFPS-Decky", "--max-time", str(timeout)]
    if dest:
        cmd += ["-o", dest]
    cmd.append(url)
    r = subprocess.run(cmd, env=_env(), capture_output=True, timeout=timeout + 10)
    if r.returncode != 0:
        raise RuntimeError(f"не вдалося завантажити {url}: {r.stderr.decode(errors='ignore')[:200]}")
    return None if dest else r.stdout.decode("utf-8", errors="ignore")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extractor():
    for tool in ("7z", "7za", "7zz", "bsdtar"):
        p = shutil.which(tool)
        if p:
            return tool, p
    for p in ("/usr/bin/7z", "/usr/lib/p7zip/7z", "/usr/bin/bsdtar"):
        if os.path.isfile(p):
            return os.path.basename(p), p
    return None, None


def extract(archive, out_dir):
    tool, path = extractor()
    if not path:
        raise RuntimeError("на пристрої немає 7z/bsdtar для розпакування OptiScaler")
    os.makedirs(out_dir, exist_ok=True)
    if tool == "bsdtar":
        cmd = [path, "-xf", archive, "-C", out_dir]
    else:
        cmd = [path, "x", "-y", f"-o{out_dir}", archive]
    rc, out = _run(cmd, timeout=300)
    if rc != 0:
        raise RuntimeError(f"розпакування не вдалося: {out[:300]}")


# ------------------------------------------------------------------ оновлення

def check_latest(force=False):
    """Остання стабільна версія OptiScaler (кеш 6 год). Повертає стан."""
    st = load_state()
    if not force and time.time() - st.get("checked_at", 0) < UPDATE_TTL and st.get("latest_tag"):
        return st
    try:
        data = json.loads(http_get(API_LATEST, timeout=20))
        assets = [a for a in data.get("assets", [])
                  if a.get("name", "").lower().endswith(".7z") and "optiscaler" in a["name"].lower()]
        if not assets:
            raise RuntimeError("у релізі немає архіву OptiScaler")
        asset = assets[0]
        digest = str(asset.get("digest") or "")
        st.update(latest_tag=data.get("tag_name"), latest_name=data.get("name") or data.get("tag_name"),
                  latest_url=asset["browser_download_url"], latest_asset=asset["name"],
                  latest_sha256=digest.split(":", 1)[1] if digest.startswith("sha256:") else "",
                  latest_published=data.get("published_at", ""), checked_at=int(time.time()),
                  check_error="")
    except Exception as e:  # noqa: BLE001
        st.update(checked_at=int(time.time()), check_error=str(e)[:300])
    save_state(st)
    return st


def update_available(st=None):
    st = st or load_state()
    latest, current = st.get("latest_tag"), installed_tag()
    return bool(latest and latest != current)


def download_latest(log=print):
    """Завантажує й розпаковує останню версію; лишає попередню до успіху."""
    st = check_latest(force=True)
    if not st.get("latest_url"):
        raise RuntimeError(st.get("check_error") or "не вдалося дізнатися останню версію OptiScaler")
    tag = st["latest_tag"]
    dest_dir = version_dir(tag)
    tmp_dir = dest_dir + ".tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(os.path.join(C.CACHE_DIR, "optiscaler"), exist_ok=True)
    archive = os.path.join(C.CACHE_DIR, "optiscaler", st["latest_asset"])
    log(f"завантажую {st['latest_asset']}")
    http_get(st["latest_url"], archive, timeout=600)
    if st.get("latest_sha256") and sha256(archive) != st["latest_sha256"]:
        os.remove(archive)
        raise RuntimeError("контрольна сума архіву OptiScaler не збігається — завантаження відкинуто")
    extract(archive, tmp_dir)
    # архів може містити вкладену теку — шукаємо OptiScaler.dll
    found = glob.glob(os.path.join(tmp_dir, "**", "OptiScaler.dll"), recursive=True)
    if not found:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("в архіві немає OptiScaler.dll")
    src = os.path.dirname(found[0])
    shutil.rmtree(dest_dir, ignore_errors=True)
    shutil.move(src, dest_dir)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    try:
        os.remove(archive)
    except OSError:
        pass
    prev = st.get("installed_tag")
    st.update(installed_tag=tag, installed_at=int(time.time()))
    save_state(st)
    # стару версію прибираємо лише після успіху
    if prev and prev != tag:
        shutil.rmtree(version_dir(prev), ignore_errors=True)
    log(f"OptiScaler {tag} готовий")
    return tag


def fetch_asset(key, log=print):
    """Завантажує додатковий файл FSR 4 один раз; перевіряє sha256."""
    a = ASSETS[key]
    dest = os.path.join(root_dir(), "assets", key, a["file"])
    if os.path.isfile(dest) and sha256(dest) == a["sha256"]:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    log(f"завантажую {key}")
    http_get(a["url"], tmp, timeout=300)
    if sha256(tmp) != a["sha256"]:
        os.remove(tmp)
        raise RuntimeError(f"контрольна сума {key} не збігається — файл відкинуто")
    os.replace(tmp, dest)
    return dest


def _version_tuple(tag):
    nums = [int(x) for x in re.findall(r"\d+", tag or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


# ------------------------------------------------------------------ відеокарта

GPU_IDS = {
    # (vendor 0x1002) device id → архітектура
    "0x163f": ("rdna2", "Steam Deck (Van Gogh)"), "0x1435": ("rdna2", "Steam Deck OLED (Sephiroth)"),
    "0x1681": ("rdna2", "Rembrandt / Ryzen Z2 Go"), "0x15bf": ("rdna3", "Phoenix / Ryzen Z1 Extreme"),
    "0x15c8": ("rdna3", "Phoenix2 / Ryzen Z1"), "0x150e": ("rdna3.5", "Strix Point / Z2 Extreme"),
    "0x1114": ("rdna3.5", "Krackan"),
}


def detect_gpu():
    for dev in sorted(glob.glob("/sys/class/drm/card*/device")):
        vendor = C.read_text(os.path.join(dev, "vendor")).lower()
        device = C.read_text(os.path.join(dev, "device")).lower()
        if vendor == "0x1002":
            arch, name = GPU_IDS.get(device, ("unknown", f"AMD {device}"))
            if arch == "unknown":
                # грубе визначення за назвою драйвера/ІД: RDNA4 — 0x75xx/0x7550+
                if device.startswith("0x75") or device.startswith("0x7d"):
                    arch = "rdna4"
            return {"vendor": "amd", "arch": arch, "name": name, "device": device}
        if vendor == "0x10de":
            return {"vendor": "nvidia", "arch": "nvidia", "name": "NVIDIA", "device": device}
    return {"vendor": "unknown", "arch": "unknown", "name": "невідомо", "device": ""}


def effective_gpu(settings=None):
    """Автовизначення + ручне перевизначення архітектури з налаштувань."""
    gpu = dict(detect_gpu())
    forced = (settings or {}).get("opti_gpu_arch", "auto")
    if forced in ("rdna2", "rdna3", "rdna4"):
        gpu.update(vendor="amd", arch=forced, forced=True)
    return gpu


def fsr4_variant(gpu, choice):
    if choice == "fsr40":
        return "rdna23-int8-402"
    arch = gpu.get("arch")
    if arch == "rdna2":
        return "rdna2-valve-411"
    if arch == "rdna4":
        return "rdna4-native"
    return "rdna34-official-411"  # RDNA3 / 3.5 і невідомі AMD


def resolve_upscaler(choice, gpu):
    """auto → FSR 4.1 на AMD. Повертає (ключ, підпис)."""
    if choice in ("fsr31", "xess", "fsr4", "fsr40"):
        key = choice
    else:
        key = "fsr4" if gpu.get("vendor") == "amd" else "fsr31"
    if key in ("fsr4", "fsr40"):
        return key, FSR4_VARIANTS[fsr4_variant(gpu, key)]["label"]
    return key, {"fsr31": "FSR 3.1", "xess": "XeSS"}[key]


# ------------------------------------------------------------------ гра: рушій і тека

def detect_engine(install):
    if glob.glob(os.path.join(install, "*", "Binaries", "Win64", "*Shipping*.exe")) or \
            glob.glob(os.path.join(install, "*", "Binaries", "WinGDK", "*Shipping*.exe")) or \
            os.path.isdir(os.path.join(install, "Engine", "Binaries")):
        return "unreal"
    if glob.glob(os.path.join(install, "re_chunk_000.pak*")) or \
            glob.glob(os.path.join(install, "*", "re_chunk_000.pak*")):
        return "re_engine"
    if glob.glob(os.path.join(install, "UnityPlayer.dll")) or \
            glob.glob(os.path.join(install, "*_Data", "app.info")):
        return "unity"
    return "generic"


def has_anticheat(install):
    for m in ANTICHEAT_MARKERS:
        if glob.glob(os.path.join(install, m)) or glob.glob(os.path.join(install, "*", m)) or \
                glob.glob(os.path.join(install, "*", "*", m)):
            return m
    return None


def _exe_score(exe, install, game_name):
    norm = exe.replace("\\", "/").lower()
    name = os.path.basename(norm)
    score = 0
    if norm.endswith("-win64-shipping.exe"):
        score += 300
    if "shipping.exe" in name:
        score += 220
    if "/binaries/win64/" in norm:
        score += 200
    if "/win64/" in norm:
        score += 80
    if os.path.dirname(exe) == install:
        score += 20
    g = re.sub(r"[^a-z0-9]", "", (game_name or "").lower())
    n = re.sub(r"[^a-z0-9]", "", os.path.splitext(name)[0])
    r = re.sub(r"[^a-z0-9]", "", os.path.basename(install).lower())
    if g and g in n:
        score += 120
    if r and r in n:
        score += 90
    if any(b in norm for b in BAD_EXE):
        score -= 200
    score -= norm.count("/")
    return score


def find_target(appid, install, game_name="", args=None):
    """Тека з основним .exe гри, куди кладеться OptiScaler."""
    if appid in EXE_FIXUPS and os.path.isdir(os.path.join(install, EXE_FIXUPS[appid])):
        return os.path.join(install, EXE_FIXUPS[appid])
    engine = detect_engine(install)
    if engine == "unreal":
        ships = [p for p in glob.glob(os.path.join(install, "*", "Binaries", "Win*", "*Shipping*.exe"))
                 if "/Engine/" not in p]
        if ships:
            return os.path.dirname(sorted(ships)[0])
    # реальний .exe з команди запуску (якщо відомий і не лаунчер)
    for a in args or []:
        if a.lower().endswith(".exe") and a.startswith(install) and os.path.isfile(a) \
                and not any(b in a.lower() for b in BAD_EXE):
            return os.path.dirname(a)
    exes = []
    for depth in ("*.exe", "*/*.exe", "*/*/*.exe", "*/*/*/*.exe", "*/*/*/*/*.exe"):
        exes += glob.glob(os.path.join(install, depth))
    if not exes:
        return install
    best = max(exes, key=lambda e: _exe_score(e, install, game_name))
    return os.path.dirname(best)


# ------------------------------------------------------------------ ini

def ini_set(path, overrides):
    """Оновлює OptiScaler.ini: {"Секція.Ключ": "значення"} або {"Ключ": …}."""
    with open(path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    nl = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines()
    sec_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")

    def upsert(section, key, value):
        # спершу шукаємо ключ будь-де: назви секцій між версіями OptiScaler змінюються
        kre = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for i, line in enumerate(lines):
            if kre.match(line):
                lines[i] = f"{key}={value}"
                return
        if section is None:
            lines.append(f"{key}={value}")
            return
        for i, line in enumerate(lines):
            m = sec_re.match(line)
            if m and m.group(1) == section:
                lines.insert(i + 1, f"{key}={value}")
                return
        lines.extend(["", f"[{section}]", f"{key}={value}"])

    for k, v in overrides.items():
        section, key = k.split(".", 1) if "." in k else (None, k)
        upsert(section, key, v)
    with open(path, "w", encoding="utf-8") as f:
        f.write(nl.join(lines) + nl)


def ini_overrides(engine, appid, upscaler, opti_fg, variant=None):
    ov = {
        "Menu.UseHQFont": "false",          # на Proton автоматичний шрифт падає (Decky Framegen)
        # DLSS-ігри здебільшого DX12; FSR 4 = шлях FSR 3.1 + оновлення до FSR 4
        "Upscalers.Dx12Upscaler": "fsr31" if upscaler in ("fsr4", "fsr40") else upscaler,
        "FSR.Fsr4Update": "true" if upscaler in ("fsr4", "fsr40") else "false",
    }
    if opti_fg:
        ov.update({"FrameGen.FGInput": "nukems", "FrameGen.FGOutput": "nukems"})
    else:
        ov.update({"FrameGen.FGInput": "nofg", "FrameGen.FGOutput": "nofg"})
    if variant:
        ov.update(FSR4_VARIANTS[variant]["ini"])
    ov.update(ENGINE_PROFILES[engine]["ini"])
    ov.update(GAME_PROFILES.get(str(appid), {}).get("ini", {}))
    return ov


# ------------------------------------------------------------------ встановлення в гру

def _files_to_copy(src):
    """Усе, що потрібно OptiScaler: DLL/ini/asi у корені + теки D3D12_Optiscaler, plugins."""
    out = []
    for name in sorted(os.listdir(src)):
        p = os.path.join(src, name)
        if os.path.isdir(p):
            if name in ("D3D12_Optiscaler", "plugins"):
                out.append(name)
            continue
        if os.path.splitext(name)[1].lower() in SKIP_EXT:
            continue
        out.append(name)
    return out


def install(appid, install_path, game_name="", args=None, settings=None, log=print):
    """Встановлює OptiScaler у гру. Повертає словник стану гри."""
    settings = settings or C.load_json(C.SETTINGS_FILE, {})
    appid = str(appid)
    prof = GAME_PROFILES.get(appid, {})
    if prof.get("unsupported"):
        raise RuntimeError(prof["unsupported"])
    if not install_path or not os.path.isdir(install_path):
        raise RuntimeError("не знайдено теку гри")
    ac = has_anticheat(install_path)
    if ac:
        raise RuntimeError(f"у грі є античит ({ac}) — підміна бібліотек може призвести до бану, "
                           "тому OptiScaler тут не вмикається")
    tag = installed_tag()
    if not tag:
        raise RuntimeError("OptiScaler ще не завантажено")
    src = version_dir(tag)
    engine = detect_engine(install_path)
    target = find_target(appid, install_path, game_name, args)
    if not os.access(target, os.W_OK):
        raise RuntimeError(f"немає прав на запис у теку гри: {target}")

    game = load_game(appid)
    if game.get("target") and game["target"] != target and game.get("installed"):
        _remove_files(game)  # тека змінилась — прибрати старе
    dll = game.get("dll") or ENGINE_PROFILES[engine]["dll"]
    gpu = effective_gpu(settings)
    upscaler, up_label = resolve_upscaler(settings.get("opti_upscaler", "auto"), gpu)
    opti_fg = bool(settings.get("opti_fg", False))
    variant = fsr4_variant(gpu, upscaler) if upscaler in ("fsr4", "fsr40") else None
    vinfo = FSR4_VARIANTS.get(variant, {})
    # усі додаткові файли — заздалегідь, щоб не лишити гру напіввстановленою
    extra_paths = {k: fetch_asset(k, log) for k in vinfo.get("extra", [])}
    up_override = fetch_asset(vinfo["upscaler"], log) if vinfo.get("upscaler") else None
    injector = None
    if vinfo.get("injector_pre10") and _version_tuple(tag) < (0, 10, 0):
        injector = fetch_asset("optiscaler_pre10", log)

    # 0) прибрати файли попереднього встановлення (інший варіант FSR 4 / стара версія),
    #    бекапи оригіналів гри (*.b) при цьому не чіпаємо
    for name in game.get("files", []) if game.get("installed") and game.get("target") == target else []:
        p = os.path.join(target, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            os.remove(p)
    backups = list(game.get("backups", []))
    # 1) бекап оригінальних файлів гри (лише ті, що не наші)
    ours = set(game.get("files", []))
    for name in ORIGINAL_BACKUPS:
        p = os.path.join(target, name)
        if os.path.isfile(p) and name not in ours and not os.path.exists(p + ".b"):
            shutil.move(p, p + ".b")
            backups.append(name)
    # 2) копіювання файлів OptiScaler
    files = []
    for name in _files_to_copy(src):
        s = os.path.join(src, name)
        if name == "OptiScaler.dll":
            d = os.path.join(target, dll)
            files.append(dll)
            if injector:
                s = injector
        else:
            d = os.path.join(target, name)
            files.append(name)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
    # 3) FSR 4: файли варіанта під відеокарту
    for key, path in extra_paths.items():
        name = ASSETS[key]["file"]
        shutil.copy2(path, os.path.join(target, name))
        files.append(name)
    if up_override:
        shutil.copy2(up_override, os.path.join(target, "amd_fidelityfx_upscaler_dx12.dll"))
        files.append("amd_fidelityfx_upscaler_dx12.dll")
    # 4) налаштування
    ini = os.path.join(target, "OptiScaler.ini")
    if os.path.isfile(ini):
        ini_set(ini, ini_overrides(engine, appid, upscaler, opti_fg, variant))
    marker = {"appid": appid, "tag": tag, "dll": dll, "files": files, "backups": backups,
              "installed_at": int(time.time())}
    with open(os.path.join(target, MARKER), "w", encoding="utf-8") as f:
        json.dump(marker, f, indent=2)
    files.append(MARKER)

    game.update(installed=True, enabled=True, tag=tag, target=target, install=install_path,
                dll=dll, engine=engine, files=sorted(set(files)), backups=sorted(set(backups)),
                upscaler=upscaler, upscaler_label=up_label, opti_fg=opti_fg, gpu=gpu["name"],
                fsr4_variant=variant or "", injector="0.10.0-pre1" if injector else tag,
                name=game_name or game.get("name", ""), updated=int(time.time()), error="")
    save_game(appid, game)
    log(f"OptiScaler {tag} → {target} ({dll}, {ENGINE_PROFILES[engine]['label']}, {up_label})")
    return game


def _remove_files(game):
    target = game.get("target")
    if not target or not os.path.isdir(target):
        return
    for name in game.get("files", []):
        p = os.path.join(target, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)
    for name in ("OptiScaler.log", "fakenvapi.log", "dlssg_to_fsr3.log"):
        p = os.path.join(target, name)
        if os.path.exists(p):
            os.remove(p)
    for name in game.get("backups", []):
        b = os.path.join(target, name + ".b")
        if os.path.exists(b):
            dest = os.path.join(target, name)
            if os.path.exists(dest):
                os.remove(dest)
            shutil.move(b, dest)


def uninstall(appid, log=print):
    appid = str(appid)
    game = load_game(appid)
    if not game:
        return {}
    _remove_files(game)
    game.update(installed=False, enabled=False, files=[], backups=[], updated=int(time.time()))
    save_game(appid, game)
    log(f"OptiScaler прибрано з {game.get('target')}, оригінальні файли повернуто")
    return game


def ensure(appid, args, settings=None, log=print):
    """Перед запуском гри: перевстановити, якщо файли зникли (оновлення гри)
    або вийшла нова версія OptiScaler. Повертає (dll_stem | None, opti_fg)."""
    appid = str(appid)
    game = load_game(appid)
    if not game.get("enabled"):
        return None, False
    settings = settings or C.load_json(C.SETTINGS_FILE, {})
    target = game.get("target", "")
    missing = not os.path.isfile(os.path.join(target, game.get("dll", "dxgi.dll"))) or \
        not os.path.isfile(os.path.join(target, MARKER))
    outdated = installed_tag() and game.get("tag") != installed_tag()
    gpu = effective_gpu(settings)
    want_up = resolve_upscaler(settings.get("opti_upscaler", "auto"), gpu)[0]
    want_var = fsr4_variant(gpu, want_up) if want_up in ("fsr4", "fsr40") else ""
    changed = (game.get("upscaler"), game.get("opti_fg"), game.get("fsr4_variant", "")) != (
        want_up, bool(settings.get("opti_fg", False)), want_var)
    if missing or outdated or changed:
        why = "файли зникли (оновлення гри?)" if missing else \
            "нова версія OptiScaler" if outdated else "змінено налаштування"
        log(f"OptiScaler: перевстановлюю — {why}")
        try:
            game = install(appid, game.get("install"), game.get("name", ""), args, settings, log)
        except Exception as e:  # noqa: BLE001
            game["error"] = str(e)
            save_game(appid, game)
            log(f"OptiScaler: не вдалося — {e}; гра запуститься без нього")
            return None, False
    dll = game.get("dll", "dxgi.dll")
    stem = None if dll.endswith(".asi") else os.path.splitext(dll)[0]
    return stem, bool(game.get("opti_fg"))


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "ensure":
        try:
            stem, fg = ensure(sys.argv[2], sys.argv[3:], log=lambda m: print(m, file=sys.stderr))
        except Exception as e:  # noqa: BLE001
            print(f"OptiScaler error: {e!r}", file=sys.stderr)
            stem, fg = None, False
        print(f"DLL={stem or ''}")
        print(f"FG={1 if fg else 0}")


if __name__ == "__main__":
    main()
