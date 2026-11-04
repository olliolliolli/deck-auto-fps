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

# ---- «Один файл» (як у Deck Wizard): гра сама має FSR 3.1 (Capcom: MH Wilds, RE Requiem,
# Dragon's Dogma 2, Pragmata… і багато інших) → кладемо поруч бібліотеку драйвера FSR 4
# amdxcffx64.dll, і вбудований у гру FSR 3.1 стає FSR 4. OptiScaler і REFramework не потрібні.
FFX_DLLS = ["amd_fidelityfx_dx12.dll", "amd_fidelityfx_upscaler_dx12.dll",
            "amd_fidelityfx_loader_dx12.dll"]
DIRECT_VARIANTS = {
    # саме той «один файл»: збірка Valve для не-RDNA4, працює з вбудованим amdxc64 Proton
    "valve": {"label": "FSR 4.1.1 · один файл (Valve)", "files": ["amdxcffx64_valve_411"],
              "override": ""},
    # якщо вбудований amdxc64 Proton старий — власна прокладка amdxc64 поруч
    "valve_shim": {"label": "FSR 4.1.1 · файл Valve + amdxc64",
                   "files": ["amdxcffx64_valve_411", "amdxc64_rdna2"], "override": "amdxc64"},
    "official": {"label": "FSR 4.1.1 · один файл (AMD)", "files": ["amdxcffx64_official_411"],
                 "override": ""},
    "official_shim": {"label": "FSR 4.1.1 · файл AMD + amdxc64",
                      "files": ["amdxcffx64_official_411", "amdxc64_rdna2"], "override": "amdxc64"},
}
DIRECT_MARKER = "AUTOFPS_FSR4.json"


def direct_chain(gpu):
    """Порядок спроб «одного файлу» під відеокарту; після них — OptiScaler."""
    if gpu.get("arch") == "rdna4":
        return ["official"]
    if gpu.get("arch") == "rdna2":
        return ["valve", "valve_shim"]
    return ["valve", "valve_shim", "official_shim"]   # RDNA3 / 3.5 (Legion Go S на Z1E)

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
    # Capcom RE Engine без вбудованого DLSS: REFramework (гілка pd-upscaler, збірка під гру)
    # + PureDark UpscalerBasePlugin (PDPerfPlugin.dll) + OptiScaler. Вікі OptiScaler.
    # RE4: власний міст FSR2→FSR4 (інтерфейс чіткий); REFramework — запасний шлях
    "2050650": {"bridge": "re4", "ref": "pd", "ref_asset": "RE4.zip", "name": "Resident Evil 4 (2023)"},
    "883710": {"ref": "pd", "ref_asset": "RE2.zip", "name": "Resident Evil 2"},
    "952060": {"ref": "pd", "ref_asset": "RE3.zip", "name": "Resident Evil 3"},
    "418370": {"ref": "pd", "ref_asset": "RE7.zip", "name": "Resident Evil 7"},
    # у Village FSR 4 дає зіпсовану картинку (вікі) — лише FSR 3.1 / XeSS
    "1196590": {"ref": "pd", "ref_asset": "RE8.zip", "name": "Resident Evil Village",
                "max_upscaler": "fsr31"},
    "601150": {"ref": "pd", "ref_asset": "DMC5.zip", "name": "Devil May Cry 5"},
    # Capcom з вбудованим DLSS
    "2246340": {"ref": "nightly"},                                    # Monster Hunter Wilds
    "1446780": {"ini": {"Hooks.RestoreComputeSignature": "true"}},   # Monster Hunter Rise
    "2054970": {},                                                    # Dragon's Dogma 2
}

# REFramework: дві різні збірки (як у decky-optiscaler-plus)
REF_PD_API = "https://api.github.com/repos/TheRazerMD/REFramework/releases"
REF_PD_PAGE = "https://github.com/TheRazerMD/REFramework/releases"
REF_NIGHTLY_API = "https://api.github.com/repos/praydog/REFramework-nightly/releases/latest"
# вікі: нові PD-збірки зламані в DMC5 — остання робоча
REF_PINNED = {
    "DMC5.zip": {"url": "https://github.com/user-attachments/files/26169274/DMC5_working.zip",
                 "sha256": "8f258480f390a3d063731805c00f58f0246be35580e600747f3c00d917ec225c",
                 "tag": "DMC5_working (вікі)"},
}
# PureDark UpscalerBasePlugin: лише на Nexus Mods за входом — не завантажується автоматично
PD_PLUGIN = "PDPerfPlugin.dll"
PD_PLUGIN_NAME = "UpscalerBasePlugin 1.1.2"
PD_PLUGIN_URL = "https://www.nexusmods.com/site/mods/502?tab=files"
PD_PLUGIN_SHA256 = "2e3b10d46a587af3294ff1d02244ea410441befd1f8eaff329ff3f968d2d9a7f"  # 1.1.2
REF_OVERRIDE = "dinput8"
# REFramework масштабує кадр РАЗОМ з інтерфейсом (issue praydog/REFramework#670) — текст
# розмивається тим більше, чим нижча внутрішня роздільність. Тому для цих ігор фіксуємо
# співвідношення масштабування OptiScaler: 1.3 ≈ рендер 77% (чіткий текст), 1.0 — FSR 4 як AA.
REF_RATIOS = {"sharp": "1.3", "native": "1.0", "auto": None}


def ref_ratio(settings):
    return REF_RATIOS.get((settings or {}).get("ref_sharpness", "sharp"), "1.3")   # Proton має власний dinput8 — без override REFramework не завантажиться

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
    if not st.get("latest_sha256"):
        log("увага: GitHub не надав контрольну суму архіву — перевірку sha256 пропущено")
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


def find_ffx(install, max_depth=5):
    """Теки гри з власним FSR 3.1+ (бібліотеки FidelityFX API). Порожньо — у гри FSR 3.1 немає."""
    out = []
    if not install or not os.path.isdir(install):
        return out
    base = install.rstrip("/").count("/")
    for root, dirs, files in os.walk(install):
        if root.count("/") - base >= max_depth:
            dirs[:] = []
        low = {f.lower() for f in files}
        if any(n in low for n in FFX_DLLS):
            out.append(root)
    return sorted(out, key=lambda p: (p.count("/"), p))


METHOD_LABELS = {"optiscaler": "OptiScaler (DLSS → FSR 4)", "direct": "Один файл (FSR 3.1 гри → FSR 4)",
                 "bridge": "Міст AutoFPS (FSR 2 гри → FSR 4)"}


def method_options(appid, install):
    """Способи FSR 4 для вибору вручну: [{id, label, ok, why}]. ok=False — у цій грі не спрацює."""
    has_dir = bool(install and os.path.isdir(install))
    prof = GAME_PROFILES.get(str(appid), {})
    engine = detect_engine(install) if has_dir else "generic"
    ffx = find_ffx(install) if has_dir else []
    out = []
    out.append({"id": "optiscaler", "label": METHOD_LABELS["optiscaler"],
                "ok": not prof.get("unsupported"),
                "why": prof.get("unsupported", "") or "Гра має підтримувати DLSS (або XeSS/FSR 2 у меню)"})
    out.append({"id": "direct", "label": METHOD_LABELS["direct"], "ok": bool(ffx),
                "why": "" if ffx else "У грі немає власного FSR 3.1"})
    bridge_ok = bool(bridge_dll()) and (bool(prof.get("bridge")) or engine == "re_engine") and not ffx
    out.append({"id": "bridge", "label": METHOD_LABELS["bridge"], "ok": bridge_ok,
                "why": "" if bridge_ok else "Лише для ігор RE Engine з FSR 2 (RE2/RE3/RE4/RE7/RE8…)"})
    return out


def choose_method(appid, install, settings, gpu):
    """'direct' — «один файл» у вбудований FSR 3.1 гри; 'optiscaler' — підміна DLSS."""
    forced = (settings or {}).get("opti_method", "auto")
    if forced in ("direct", "optiscaler"):
        return forced
    if gpu.get("vendor") not in ("amd", "unknown"):
        return "optiscaler"
    prof = GAME_PROFILES.get(str(appid), {})
    if prof.get("bridge") and bridge_dll():
        return "bridge"                # FSR 2 гри → FSR 4 нашим мостом
    # інші ігри RE Engine (Capcom): міст сам знаходить кроки FSR 2 у кадрі, без хешів конкретної гри
    if bridge_dll() and not prof.get("max_upscaler") and install and not find_ffx(install) \
            and detect_engine(install) == "re_engine":
        return "bridge"
    if (settings or {}).get("opti_upscaler", "auto") not in ("auto", "fsr4"):
        return "optiscaler"            # FSR 3.1 / XeSS / FSR 4.0 — це вміє лише OptiScaler
    if (settings or {}).get("opti_fg"):
        return "optiscaler"            # генерація кадрів OptiScaler
    return "direct" if find_ffx(install) else "optiscaler"


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


def ffx_key(src_ini):
    """Як ця версія OptiScaler називає FSR 3.1/4 у Dx12Upscaler: 'fsr31' (0.9.x) чи 'ffx' (новіші).
    Беремо з блоку коментарів над Dx12Upscaler у самому ini версії."""
    block = []
    try:
        with open(src_ini, encoding="utf-8", errors="ignore") as f:
            for line in f:
                st = line.strip()
                if st.startswith("Dx12Upscaler"):
                    break
                block = block + [st] if st.startswith(";") else []
    except OSError:
        pass
    text = " ".join(block)
    if re.search(r"\bffx\b", text) and not re.search(r"\bfsr31\b", text):
        return "ffx"
    return "fsr31"


def ini_overrides(engine, appid, upscaler, opti_fg, variant=None, ffx="fsr31"):
    fsr = upscaler in ("fsr4", "fsr40", "fsr31")
    ov = {
        # журнал для самоперевірки після гри (Info — без зайвого навантаження)
        "Log.LogToFile": "true",
        "Log.LogLevel": "2",
        "Log.SingleFile": "true",
        "Menu.UseHQFont": "false",          # на Proton автоматичний шрифт падає (Decky Framegen)
        # DLSS-ігри здебільшого DX12. FSR 3.1/4 — один шлях; бекенд задає UpscalerIndex:
        # 0 = FSR 4, 1 = FSR 3.1 (за замовчуванням на не-RDNA4 OptiScaler бере FSR 3!)
        "Upscalers.Dx12Upscaler": ffx if fsr else upscaler,
        "FSR.Fsr4Update": "true" if upscaler in ("fsr4", "fsr40") else "false",
    }
    if fsr:
        ov["FSR.UpscalerIndex"] = "1" if upscaler == "fsr31" else "0"
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


# ------------------------------------------------------------------ REFramework (Capcom)

class NeedPdPlugin(RuntimeError):
    """Бракує PDPerfPlugin.dll — користувач має завантажити його з Nexus."""


def pd_store():
    return os.path.join(root_dir(), "pd", PD_PLUGIN)


def _scan_dirs():
    dirs = [os.path.join(C.HOME, "Downloads"), os.path.join(C.HOME, "Desktop")]
    dirs += glob.glob("/run/media/*/*") + glob.glob("/run/media/*")
    return [d for d in dirs if os.path.isdir(d)]


def _walk(base, depth=3):
    base_depth = base.rstrip("/").count("/")
    for root, dirs, files in os.walk(base):
        if root.count("/") - base_depth >= depth:
            dirs[:] = []
        yield root, files


def _from_archive(path, dest):
    """Дістати PDPerfPlugin.dll з архіву (zip — сам, 7z/rar — через 7z/bsdtar)."""
    import zipfile
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if os.path.basename(n).lower() == PD_PLUGIN.lower():
                    with z.open(n) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                    return True
        return False
    tmp = dest + ".x"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        extract(path, tmp)
        found = [p for p in glob.glob(os.path.join(tmp, "**", "*"), recursive=True)
                 if os.path.basename(p).lower() == PD_PLUGIN.lower()]
        if found:
            shutil.copy2(found[0], dest)
            return True
    except Exception:  # noqa: BLE001
        pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return False


def import_pd_plugin(path):
    """Взяти PDPerfPlugin.dll з указаного файлу (DLL або архів) у сховище плагіна."""
    dest = pd_store()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".new"
    if os.path.basename(path).lower() == PD_PLUGIN.lower():
        shutil.copy2(path, tmp)
    elif not _from_archive(path, tmp):
        raise RuntimeError(f"у {os.path.basename(path)} немає {PD_PLUGIN}")
    os.replace(tmp, dest)
    return pd_status()


def find_pd_plugin():
    """Шукає PDPerfPlugin.dll (або архів з ним) у Завантаженнях і на картках пам'яті."""
    if os.path.isfile(pd_store()):
        return pd_status()
    for base in _scan_dirs():
        for root, files in _walk(base):
            for f in files:
                low = f.lower()
                p = os.path.join(root, f)
                if low == PD_PLUGIN.lower():
                    return import_pd_plugin(p)
                if low.endswith((".zip", ".7z", ".rar")) and ("upscaler" in low or "pdperf" in low
                                                                or "puredark" in low):
                    try:
                        return import_pd_plugin(p)
                    except Exception:  # noqa: BLE001
                        continue
    return pd_status()


def pd_status():
    p = pd_store()
    if not os.path.isfile(p):
        return {"present": False, "verified": False, "name": PD_PLUGIN_NAME, "url": PD_PLUGIN_URL}
    return {"present": True, "verified": sha256(p) == PD_PLUGIN_SHA256, "name": PD_PLUGIN_NAME,
            "url": PD_PLUGIN_URL}


def _release_asset(release, name):
    for a in release.get("assets", []):
        if a.get("name", "").lower() == name.lower():
            d = str(a.get("digest") or "")
            return {"url": a["browser_download_url"], "tag": release.get("tag_name", ""),
                    "sha256": d.split(":", 1)[1] if d.startswith("sha256:") else ""}
    return None


def fetch_reframework(kind, asset, log=print):
    """Завантажує REFramework (pd — збірка під гру, nightly — уніфікована). Повертає (шлях до dinput8.dll, тег)."""
    import zipfile
    if kind == "pd" and asset in REF_PINNED:
        info = REF_PINNED[asset]
    elif kind == "pd":
        info = None
        for rel in json.loads(http_get(REF_PD_API, timeout=30)):
            info = _release_asset(rel, asset)
            if info:
                break
        if not info:
            raise RuntimeError(f"у релізах REFramework (pd-upscaler) немає {asset}: {REF_PD_PAGE}")
    else:
        info = _release_asset(json.loads(http_get(REF_NIGHTLY_API, timeout=30)), "REFramework.zip")
        if not info:
            raise RuntimeError("не знайдено REFramework.zip у nightly")
    # збірка pd-upscaler своя для кожної гри — кеш окремо за грою, інакше чужа збірка = падіння
    game_key = os.path.splitext(asset)[0] if kind == "pd" else "unified"
    cache = os.path.join(root_dir(), "reframework", kind, game_key,
                         re.sub(r"[^\w.-]", "_", info["tag"]))
    dll = os.path.join(cache, "dinput8.dll")
    if os.path.isfile(dll):
        return dll, info["tag"]
    os.makedirs(cache, exist_ok=True)
    arc = os.path.join(cache, asset if kind == "pd" else "REFramework.zip")
    log(f"завантажую REFramework {info['tag']} ({os.path.basename(arc)})")
    http_get(info["url"], arc, timeout=300)
    if info.get("sha256") and sha256(arc) != info["sha256"]:
        os.remove(arc)
        raise RuntimeError("контрольна сума REFramework не збігається — завантаження відкинуто")
    with zipfile.ZipFile(arc) as z:
        names = [n for n in z.namelist() if os.path.basename(n).lower() == "dinput8.dll"]
        if not names:
            raise RuntimeError("в архіві REFramework немає dinput8.dll")
        with z.open(names[0]) as src, open(dll, "wb") as out:
            shutil.copyfileobj(src, out)
    os.remove(arc)
    return dll, info["tag"]


def game_upscaler(appid, settings, gpu):
    """Апскейлер для гри з урахуванням обмежень профілю (напр. Village — без FSR 4)."""
    key, label = resolve_upscaler(settings.get("opti_upscaler", "auto"), gpu)
    cap = GAME_PROFILES.get(str(appid), {}).get("max_upscaler")
    if cap and key in ("fsr4", "fsr40"):
        key, label = resolve_upscaler(cap, gpu)
    return key, label


def _re_engine_disable_ingame_upscaling(install_path):
    """Вбудований FSR гри не працює як вхід для OptiScaler (вікі): вимикаємо, масштабує REFramework."""
    for ini in glob.glob(os.path.join(install_path, "*.ini")):
        if "default" in os.path.basename(ini).lower():
            continue
        try:
            text = open(ini, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        if "[AppRender]" not in text:
            continue
        new = re.sub(r"^(AppUpScalingTypeFSR[12])=\d+", r"\1=0", text, flags=re.M)
        if new != text:
            if not os.path.exists(ini + ".autofps-upscale.bak"):
                shutil.copy2(ini, ini + ".autofps-upscale.bak")
            with open(ini, "w", encoding="utf-8") as f:
                f.write(new)


def install(appid, install_path, game_name="", args=None, settings=None, log=print, reset=False, dll=None):
    """Встановлює OptiScaler у гру. Повертає словник стану гри.
    reset=True — ручне ввімкнення: почати перебір назв DLL спочатку."""
    settings = settings or C.load_json(C.SETTINGS_FILE, {})
    appid = str(appid)
    prof = GAME_PROFILES.get(appid, {})
    if prof.get("unsupported"):
        raise RuntimeError(prof["unsupported"])
    ref_kind = prof.get("ref")
    pd = find_pd_plugin() if ref_kind == "pd" else None
    if pd is not None and not pd["present"]:
        raise NeedPdPlugin(f"для {prof.get('name', 'цієї гри')} потрібен {PD_PLUGIN_NAME} з Nexus Mods")
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
    if reset:
        for k in ("dll", "dll_tried", "dll_ok", "disabled_reason", "check"):
            game.pop(k, None)
    if game.get("target") and game["target"] != target and game.get("installed"):
        _remove_files(game)  # тека змінилась — прибрати старе
    dll = dll or game.get("manual_dll") or game.get("dll") or ENGINE_PROFILES[engine]["dll"]
    game["dll"] = dll
    gpu = effective_gpu(settings)
    upscaler, up_label = game_upscaler(appid, settings, gpu)
    opti_fg = bool(settings.get("opti_fg", False))
    ref_dll = ref_tag = None
    if ref_kind:
        ref_dll, ref_tag = fetch_reframework(ref_kind, prof.get("ref_asset", ""), log)
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
    helix_here = os.path.isfile(os.path.join(target, HELIX_MARKER)) or \
        os.path.isdir(os.path.join(target, "HelixSR"))
    for name in ORIGINAL_BACKUPS + (["dinput8.dll"] if ref_kind else []):
        if helix_here and name.startswith("amd_fidelityfx"):
            continue                 # HelixSR уже стоїть у грі — його файли не чіпаємо
        p = os.path.join(target, name)
        if os.path.isfile(p) and name not in ours and not os.path.exists(p + ".b"):
            shutil.move(p, p + ".b")
            backups.append(name)
    # 2) копіювання файлів OptiScaler
    files = []
    for name in _files_to_copy(src):
        if helix_here and name.startswith("amd_fidelityfx"):
            continue
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
    if up_override and not helix_here:
        shutil.copy2(up_override, os.path.join(target, "amd_fidelityfx_upscaler_dx12.dll"))
        files.append("amd_fidelityfx_upscaler_dx12.dll")
    # 3b) REFramework (+ PDPerfPlugin) поруч з exe гри
    if ref_dll:
        shutil.copy2(ref_dll, os.path.join(target, "dinput8.dll"))
        files.append("dinput8.dll")
        if ref_kind == "pd":
            shutil.copy2(pd_store(), os.path.join(target, PD_PLUGIN))
            files.append(PD_PLUGIN)
            # TemporalUpscaler шукає libxess.dll поруч з PDPerfPlugin.dll (вікі, Opti v10)
            for lib in ("libxess.dll",):
                if not os.path.isfile(os.path.join(target, lib)):
                    sub = glob.glob(os.path.join(target, "**", lib), recursive=True)
                    if sub:
                        shutil.copy2(sub[0], os.path.join(target, lib))
                        files.append(lib)
            _re_engine_disable_ingame_upscaling(install_path)
    # 4) налаштування
    ini = os.path.join(target, "OptiScaler.ini")
    if os.path.isfile(ini):
        ov = ini_overrides(engine, appid, upscaler, opti_fg, variant,
                           ffx_key(os.path.join(src, "OptiScaler.ini")))
        if ref_kind:
            # Insert відкриває і REFramework, і OptiScaler — OptiScaler на Home (вікі)
            ov.update({"Spoofing.Dxgi": "false", "Menu.OverlayMenu": "false",
                       "Menu.ShortcutKey": "0x24"})
            ratio = ref_ratio(settings)
            ov.update({"UpscaleRatio.UpscaleRatioOverrideEnabled": "true" if ratio else "auto",
                       "UpscaleRatio.UpscaleRatioOverrideValue": ratio or "auto"})
        ini_set(ini, ov)
    marker = {"appid": appid, "tag": tag, "dll": dll, "files": files, "backups": backups,
              "installed_at": int(time.time())}
    with open(os.path.join(target, MARKER), "w", encoding="utf-8") as f:
        json.dump(marker, f, indent=2)
    files.append(MARKER)

    game.update(installed=True, enabled=True, tag=tag, target=target, install=install_path,
                dll=dll, engine=engine, files=sorted(set(files)), backups=sorted(set(backups)),
                upscaler=upscaler, upscaler_label=up_label, opti_fg=opti_fg, gpu=gpu["name"],
                fsr4_variant=variant or "", injector="0.10.0-pre1" if injector else tag,
                ref=ref_kind or "", ref_tag=ref_tag or "",
                ref_ratio=(ref_ratio(settings) or "auto") if ref_kind else "",
                pd_verified=bool(pd and pd.get("verified")),
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
    # REFramework ставили ми (не було свого dinput8.dll) — прибрати і його слід
    if game.get("ref") and "dinput8.dll" not in game.get("backups", []):
        shutil.rmtree(os.path.join(target, "reframework"), ignore_errors=True)
        for pat in ("*_fw_config.txt", "re*_framework_log.txt", "*_framework_log.txt"):
            for f in glob.glob(os.path.join(target, pat)):
                os.remove(f)
    # повернути вбудоване масштабування гри (лише ці ключі, решту налаштувань не чіпаємо)
    if game.get("ref"):
        for bak in glob.glob(os.path.join(game.get("install") or target, "*.ini.autofps-upscale.bak")):
            ini = bak[:-len(".autofps-upscale.bak")]
            try:
                old = dict(re.findall(r"^(AppUpScalingTypeFSR[12])=(\d+)", open(bak).read(), re.M))
                text = open(ini, encoding="utf-8", errors="ignore").read()
                for k, v in old.items():
                    text = re.sub(rf"^{k}=\d+", f"{k}={v}", text, flags=re.M)
                with open(ini, "w", encoding="utf-8") as f:
                    f.write(text)
                os.remove(bak)
            except OSError:
                pass
    for name in game.get("backups", []):
        b = os.path.join(target, name + ".b")
        if os.path.exists(b):
            dest = os.path.join(target, name)
            if os.path.exists(dest):
                os.remove(dest)
            shutil.move(b, dest)


# ------------------------------------------------------------------ «один файл» FSR 4

class DirectExhausted(Exception):
    """Усі варіанти «одного файлу» перепробувано — переходимо на OptiScaler."""


def _our_shas():
    return {a["sha256"] for a in ASSETS.values()}


def direct_remove(game):
    """Прибрати amdxcffx64/amdxc64, які поклали ми, і повернути файли гри (якщо були)."""
    install = game.get("install") or ""
    ours = _our_shas()
    for rel in game.get("direct_files", []):
        p = os.path.join(install, rel)
        try:
            if os.path.isfile(p) and sha256(p) in ours:
                os.remove(p)
        except OSError:
            pass
    for rel in game.get("direct_backups", []):
        p = os.path.join(install, rel)
        if os.path.isfile(p + ".autofps.b") and not os.path.exists(p):
            shutil.move(p + ".autofps.b", p)
    m = os.path.join(game.get("target") or install, DIRECT_MARKER)
    if os.path.isfile(m):
        os.remove(m)
    game.update(direct_files=[], direct_backups=[])


def direct_install(appid, install_path, game_name="", args=None, settings=None, log=print,
                   reset=False):
    """Кладе бібліотеку драйвера FSR 4 поруч з FSR 3.1 гри й поруч з її .exe."""
    settings = settings or C.load_json(C.SETTINGS_FILE, {})
    appid = str(appid)
    if not install_path or not os.path.isdir(install_path):
        raise RuntimeError("не знайдено теку гри")
    ac = has_anticheat(install_path)
    if ac:
        raise RuntimeError(f"у грі є античит ({ac}) — зміна файлів гри може призвести до бану, "
                           "тому FSR 4 тут не вмикається")
    ffx = find_ffx(install_path)
    if not ffx:
        raise RuntimeError("у грі немає власного FSR 3.1 — «один файл» тут не спрацює")
    gpu = effective_gpu(settings)
    game = load_game(appid)
    if reset:
        for k in ("direct_tried", "direct_variant", "direct_confirmed", "direct_runs",
                  "direct_exhausted", "check", "disabled_reason"):
            game.pop(k, None)
    # попереднє встановлення (OptiScaler або інший варіант) — прибрати
    if game.get("installed") and game.get("method", "optiscaler") == "optiscaler":
        _remove_files(game)
        game.update(installed=False, files=[], backups=[], ref="", ref_tag="")
    if game.get("direct_files"):
        direct_remove(game)
    chain = direct_chain(gpu)
    tried = game.get("direct_tried", [])
    cur = game.get("direct_variant")
    variant = cur if cur in chain and cur not in tried else \
        next((v for v in chain if v not in tried), None)
    if variant is None:
        game["direct_exhausted"] = True
        save_game(appid, game)
        raise DirectExhausted("жоден варіант «одного файлу» не спрацював")
    v = DIRECT_VARIANTS[variant]
    paths = {k: fetch_asset(k, log) for k in v["files"]}
    exe_dir = find_target(appid, install_path, game_name, args)
    dirs = []
    for d in [exe_dir] + ffx:
        if d not in dirs:
            dirs.append(d)
    for d in dirs:
        if not os.access(d, os.W_OK):
            raise RuntimeError(f"немає прав на запис у теку гри: {d}")
    ours = _our_shas()
    placed, backups = [], []
    for d in dirs:
        for k, src in paths.items():
            name = ASSETS[k]["file"]
            dst = os.path.join(d, name)
            rel = os.path.relpath(dst, install_path)
            if os.path.isfile(dst) and sha256(dst) not in ours \
                    and not os.path.exists(dst + ".autofps.b"):
                shutil.move(dst, dst + ".autofps.b")     # власний файл гри — зберегти
                backups.append(rel)
            shutil.copy2(src, dst)
            placed.append(rel)
    with open(os.path.join(exe_dir, DIRECT_MARKER), "w", encoding="utf-8") as f:
        json.dump({"appid": appid, "variant": variant, "files": placed}, f, indent=2)
    game.update(installed=True, enabled=True, method="direct", direct_variant=variant,
                direct_files=sorted(set(placed)),
                direct_backups=sorted(set(backups) | set(game.get("direct_backups", []))),
                override=v["override"], install=install_path, target=exe_dir,
                engine=detect_engine(install_path), upscaler="fsr4", upscaler_label=v["label"],
                fsr4_variant="direct-" + variant, gpu=gpu["name"], opti_fg=False,
                ffx_dirs=[os.path.relpath(d, install_path) for d in ffx],
                name=game_name or game.get("name", ""), updated=int(time.time()), error="")
    save_game(appid, game)
    where = ", ".join(os.path.relpath(d, install_path).replace(".", "корінь гри", 1)
                      if d == install_path else os.path.relpath(d, install_path) for d in dirs)
    log(f"FSR 4 «один файл» ({v['label']}) → {where}")
    return game


# ------------------------------------------------------------------ міст FSR2→FSR4 (RE Engine)

BRIDGE_MARK = b"AutoFPS FSR2"          # рядок усередині нашої dinput8.dll
BRIDGE_STATUS = "autofps_fsr_status.json"
BRIDGE_INI = "autofps_fsr.ini"
BRIDGE_INI_TEXT = """; AutoFPS: міст FSR2→FSR4. Файл перечитується під час гри.
enable=1
; HDR | AUTO_EXPOSURE | DEPTH_INVERTED | DEPTH_INFINITE | DEBUG_CHECKING (попередження FFX у журнал)
flags=0xB9
; очищення входів FSR 4 від NaN/Inf (сонце, небо: інакше кадр блимає чорним). 0 — вимкнути
sanitize=1
; експозиція: auto — автоекспозиція FFX, game — та, яку порахував FSR 2 гри
exposure=auto
depth_conv=0
reactive=0
skip_accumulate=1
sharpness=0.5
near=0.1
far=10000
version=4
mv_x=1
mv_y=1
jitter_x=1
jitter_y=1
"""


# ---- HelixSR (плагін «HelixSR Manager»): беремо вже зібраний ним HLX FSR 4 — AMD FSR 4 на конвеєрі
# HelixSR, розрахований на RDNA2/3. Самі нічого не завантажуємо й не збираємо (ліцензія HelixSR).
HLX_MAGIC = b"HLXFSR4D"
HELIX_MARKER = "helixsr-install.json"


def helixsr_hlx():
    """Найновіший зібраний HLX FSR 4 з ~/HelixSR/<версія>/HLX-FSR4/ (або теки з налаштувань менеджера)."""
    bases = [os.path.join(C.HOME, "HelixSR")]
    for sf in glob.glob(os.path.join(C.HOME, "homebrew", "settings", "*elix*", "settings.json")):
        d = C.load_json(sf, {}).get("helix_dir")
        if d:
            bases.insert(0, d)
    found = []
    for b in bases:
        for dll in glob.glob(os.path.join(b, "*", "HLX-FSR4", "amd_fidelityfx_upscaler_dx12.dll")):
            try:
                with open(dll, "rb") as f:
                    f.seek(-24, os.SEEK_END)
                    if f.read(8) != HLX_MAGIC:
                        continue          # шейдери ще не зібрано — у грі не запрацює
            except OSError:
                continue
            tag = os.path.basename(os.path.dirname(os.path.dirname(dll)))
            found.append((_version_tuple(tag), tag, dll))
    if not found:
        return None
    found.sort(reverse=True)
    return {"tag": found[0][1], "path": found[0][2]}


def bridge_dll():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fsr2bridge", "dinput8.dll")
    return p if os.path.isfile(p) else None


def _is_ours(path):
    try:
        with open(path, "rb") as f:
            return BRIDGE_MARK in f.read()
    except OSError:
        return False


def bridge_remove(game):
    target = game.get("target") or ""
    for name in game.get("bridge_files", []):
        p = os.path.join(target, name)
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass
    for name in game.get("bridge_backups", []):
        p = os.path.join(target, name)
        if os.path.isfile(p + ".autofps.b") and not os.path.exists(p):
            shutil.move(p + ".autofps.b", p)
    for extra in (BRIDGE_STATUS, "autofps_fsr.log"):
        try:
            os.remove(os.path.join(target, extra))
        except OSError:
            pass
    game.update(bridge_files=[], bridge_backups=[])


# RE Engine: AppUpScalingTypeFSR2 — 0 вимк., 1 «Якість», 2 «Баланс», 3 «Продуктивність», 4 «Ультрапродуктивність».
# За замовчуванням — «Продуктивність» (рендер 50%): FSR 4 сам бере свою модель для цього режиму і
# тримає картинку майже як «Якість» FSR 2, а FPS — значно вищий (як пресет Decky Framegen для handheld).
RE_FSR2_DEFAULT_MODE = 3


def _re_engine_enable_fsr2(install, mode=RE_FSR2_DEFAULT_MODE):
    """Міст працює лише коли в грі ввімкнено FSR 2: ставимо режим (типово «Продуктивність»)."""
    ini = os.path.join(install, "local_config.ini")
    try:
        text = open(ini, encoding="utf-8", errors="ignore").read()
    except OSError:
        return False
    new = re.sub(r"^AppUpScalingTypeFSR2=\d+\s*$", f"AppUpScalingTypeFSR2={mode}", text, flags=re.M)
    new = re.sub(r"^AppUpScalingTypeFSR1=[1-9]\d*\s*$", "AppUpScalingTypeFSR1=0", new, flags=re.M)
    if new != text:
        with open(ini, "w", encoding="utf-8") as f:
            f.write(new)
        return True
    return False


def bridge_install(appid, install_path, game_name="", args=None, settings=None, log=print):
    settings = settings or C.load_json(C.SETTINGS_FILE, {})
    appid = str(appid)
    if not install_path or not os.path.isdir(install_path):
        raise RuntimeError("не знайдено теку гри")
    src_bridge = bridge_dll()
    if not src_bridge:
        raise RuntimeError("у цій збірці AutoFPS немає моста FSR2→FSR4")
    game = load_game(appid)
    if game.get("installed") and game.get("method", "optiscaler") == "optiscaler":
        _remove_files(game)          # REFramework / OptiScaler з минулого разу
        game.update(installed=False, files=[], backups=[], ref="", ref_tag="")
    if game.get("direct_files"):
        direct_remove(game)
    if not installed_tag():
        download_latest(log)
    src_dir = version_dir(installed_tag())
    gpu = effective_gpu(settings)
    loader = os.path.join(src_dir, "amd_fidelityfx_dx12.dll")
    hlx = helixsr_hlx() if settings.get("bridge_version") == "hlx" else None
    if hlx:
        upscaler = hlx["path"]                      # HLX FSR 4 з HelixSR Manager
    else:
        upscaler = os.path.join(src_dir, "amd_fidelityfx_upscaler_dx12.dll") if gpu.get("arch") == "rdna4" \
            else fetch_asset("fsr40_int8", log)    # FSR 4.0.2 INT8 — працює на RDNA2/3
    if not os.path.isfile(loader):
        raise RuntimeError("у пакеті OptiScaler немає amd_fidelityfx_dx12.dll")
    target = find_target(appid, install_path, game_name, args)
    if not os.access(target, os.W_OK):
        raise RuntimeError(f"немає прав на запис у теку гри: {target}")
    files = {"dinput8.dll": src_bridge, "amd_fidelityfx_dx12.dll": loader,
             "amd_fidelityfx_upscaler_dx12.dll": upscaler}
    backups = list(game.get("bridge_backups", []))
    ours = _our_shas() | {sha256(p) for p in files.values()}
    if hlx:
        ours.add(sha256(hlx["path"]))
    for name, src in files.items():
        dst = os.path.join(target, name)
        if os.path.isfile(dst) and not _is_ours(dst) and sha256(dst) not in ours \
                and not os.path.exists(dst + ".autofps.b"):
            shutil.move(dst, dst + ".autofps.b")      # чужий файл (напр. REFramework) — зберегти
            backups.append(name)
        shutil.copy2(src, dst)
    ini = os.path.join(target, BRIDGE_INI)
    if not os.path.isfile(ini) or "AutoFPS" in C.read_text(ini)[:200]:
        with open(ini, "w", encoding="utf-8") as f:
            f.write(BRIDGE_INI_TEXT)
    bridge_set_version(target, settings)
    # ручне перевизначення з тестів більше не потрібне — його дає ensure()
    try:
        os.remove(os.path.join(C.CFG_DIR, f"dlloverride_{appid}"))
    except OSError:
        pass
    fsr2_on = _re_engine_enable_fsr2(install_path)
    game.update(installed=True, enabled=True, method="bridge", bridge_files=sorted(files) + [BRIDGE_INI],
                bridge_backups=sorted(set(backups)), install=install_path, target=target,
                engine=detect_engine(install_path), upscaler="fsr4",
                upscaler_label="FSR 4 через міст (інтерфейс чіткий)", gpu=gpu["name"], opti_fg=False,
                name=game_name or game.get("name", ""), updated=int(time.time()), error="",
                bridge_fsr2_enabled=fsr2_on)
    save_game(appid, game)
    log(f"міст FSR2→FSR4 → {target}" + (" (у грі поставлено FSR 2 «Продуктивність»)" if fsr2_on else ""))
    return game


def bridge_set_version(target, settings, log=print):
    """Налаштування «Міст: FSR 4 / FSR 3.1 / HLX FSR 4» → рядок version= у autofps_fsr.ini (міст підхопить
    на льоту). Для HLX FSR 4 ще й підміняється бібліотека апскейлера (діє з наступного запуску гри)."""
    bv = (settings or {}).get("bridge_version", "4")
    hlx = helixsr_hlx() if bv == "hlx" else None
    want = "hlx" if hlx else "3" if bv == "3" else "4"
    dst = os.path.join(target, "amd_fidelityfx_upscaler_dx12.dll")
    src = hlx["path"] if hlx else None
    if not src and os.path.isfile(dst):
        try:
            with open(dst, "rb") as f:
                f.seek(-24, os.SEEK_END)
                is_hlx = f.read(8) == HLX_MAGIC
        except OSError:
            is_hlx = False
        if is_hlx:   # був HLX — повертаємо FSR 4.0.2 INT8 від AMD
            try:
                src = fetch_asset("fsr40_int8", log)
            except Exception as e:  # noqa: BLE001
                log(f"міст: не вдалося повернути FSR 4: {e}")
    if src and os.path.isfile(dst) and sha256(src) != sha256(dst):
        tmp = dst + ".autofps-new"
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)   # новий файл: запущена гра лишає старий, нова — візьме новий
    ini = os.path.join(target, BRIDGE_INI)
    try:
        text = open(ini, encoding="utf-8").read()
    except OSError:
        return
    new = re.sub(r"^version=.*$", f"version={want}", text, flags=re.M)
    if new != text:
        with open(ini, "w", encoding="utf-8") as f:
            f.write(new)


def bridge_ensure(appid, game, args, settings, log=print):
    target = game.get("target") or ""
    bridge_set_version(target, settings)
    dll = os.path.join(target, "dinput8.dll")
    missing = not _is_ours(dll) or not os.path.isfile(os.path.join(target, "amd_fidelityfx_dx12.dll"))
    newer = bridge_dll() and os.path.isfile(dll) and sha256(dll) != sha256(bridge_dll())
    if missing or newer:
        log("міст FSR2→FSR4: перевстановлюю — " + ("файли зникли (оновлення гри?)" if missing else "нова версія моста"))
        try:
            game = bridge_install(appid, game.get("install"), game.get("name", ""), args, settings, log)
        except Exception as e:  # noqa: BLE001
            game["error"] = str(e)
            save_game(appid, game)
            log(f"міст: не вдалося — {e}; гра запуститься з рідним FSR 2")
            return None, False
    return "dinput8", False


def analyze_bridge(appid, game, started, duration, crashed, log=print):
    st_path = os.path.join(game.get("target") or "", BRIDGE_STATUS)
    st = {}
    try:
        if os.path.getmtime(st_path) >= started - 5:
            st = C.load_json(st_path, {})
    except OSError:
        pass
    res = {"t": int(time.time()), "duration": int(duration), "method": "bridge", **st}
    state = st.get("state", "")
    if crashed or duration < CRASH_SECONDS:
        game["bridge_crashes"] = int(game.get("bridge_crashes", 0)) + 1
        res.update(status="crash", title="FSR 4: гра закрилась одразу",
                   message=f"Гра закрилась через {int(duration)} с з мостом FSR2→FSR4")
        if game["bridge_crashes"] >= 2 and game.get("manual"):
            res["message"] += ". Міст вибрано вручну — лишаю; якщо гра й далі закривається, вибери інший спосіб"
        elif game["bridge_crashes"] >= 2:
            bridge_remove(game)
            game.update(enabled=False, installed=False,
                        disabled_reason="Гра двічі закрилась одразу з мостом — міст вимкнено")
            res["message"] += ". Міст вимкнено, гра працює з рідним FSR 2"
    elif state == "ok" and int(st.get("bridged", 0)) > 0:
        game["bridge_crashes"] = 0
        res["first_ok"] = not game.get("bridge_confirmed_once")
        game["bridge_confirmed_once"] = True
        res.update(status="ok", title="✓ FSR 4 працює",
                   message=f"Міст: {st.get('version', 'FSR')} замість FSR 2, інтерфейс чіткий "
                           f"({st.get('bridged')} кадрів)")
        bad = {k: int(st.get(k, 0) or 0) for k in ("bad_color", "bad_depth", "bad_mv")}
        if any(bad.values()):
            # без очищення ці пікселі давали б чорні спалахи/плями у FSR 4
            res["message"] += (f". Виправлено погані пікселі (NaN/Inf): колір {bad['bad_color']}, "
                               f"глибина {bad['bad_depth']}, вектори руху {bad['bad_mv']}")
        if int(st.get("sanitize", 1) or 0) < 0:
            res["message"] += ". Очищення входів недоступне — можливі чорні спалахи; надішли діагностику"
    elif state == "no_fsr2_shaders":
        res.update(status="error", title="FSR 4: міст не зміг підмінити FSR 2",
                   message="FSR 2 у грі знайдено автоматично, але кадри не вдалося передати в FSR 4 — "
                           "працював рідний FSR 2. Надішли діагностику")
    elif state == "fsr2_off":
        res.update(status="no_dlss", title="FSR 4: увімкни FSR 2 у грі",
                   message="Міст готовий, але гра не використовує FSR 2. Налаштування графіки → "
                           "FSR 2 (будь-який режим) — тоді замість нього працюватиме FSR 4")
    elif state in ("ffx_failed", "no_ffx"):
        res.update(status="error", title="FSR 4: FidelityFX не запустився",
                   message="Бібліотека AMD FidelityFX не створила FSR 4 — працював рідний FSR 2. "
                           "Надішли діагностику")
    else:
        res.update(status="short", title="FSR 4: замало часу для перевірки",
                   message="Пограй хоча б пару хвилин з увімкненим FSR 2")
    game["check"] = res
    save_game(appid, game)
    log(f"самоперевірка {appid} (міст): {res['status']}")
    return res if res["status"] != "ok" or res.get("first_ok") else None


def enable(appid, install_path, game_name="", args=None, settings=None, log=print, reset=True,
           method=None, dll=None):
    """method=None — спосіб вибирається сам (гра з FSR 3.1 → «один файл», RE Engine → міст, інакше
    OptiScaler). method/dll задано — вибір користувача: ставимо саме його і далі нічого не
    перемикаємо самі (лише повідомляємо, що не спрацювало)."""
    settings = settings or C.load_json(C.SETTINGS_FILE, {})
    appid = str(appid)
    gpu = effective_gpu(settings)
    manual = method is not None
    if manual and method not in METHOD_LABELS:
        raise RuntimeError(f"невідомий спосіб: {method}")
    if not manual:
        method = choose_method(appid, install_path, settings, gpu)
    g0 = load_game(appid)
    g0["manual"] = manual
    if dll and dll in DLL_CHAIN:
        g0["manual_dll"] = dll
    elif not manual:
        g0.pop("manual_dll", None)
    save_game(appid, g0)
    if method != "optiscaler" and g0.get("installed") and g0.get("method", "optiscaler") == "optiscaler":
        _remove_files(g0)                      # інший спосіб — прибрати OptiScaler з гри
        g0.update(installed=False, files=[], backups=[])
        save_game(appid, g0)
    if method == "bridge":
        g = bridge_install(appid, install_path, game_name, args, settings, log)
        g["manual"] = manual
        save_game(appid, g)
        return g
    if method == "direct" and g0.get("bridge_files"):
        bridge_remove(g0)                      # міст іншого способу — прибрати
        g0.update(installed=False)
        save_game(appid, g0)
    if method == "direct":
        try:
            g = direct_install(appid, install_path, game_name, args, settings, log, reset)
            g["manual"] = manual
            save_game(appid, g)
            return g
        except DirectExhausted:
            if manual:
                raise RuntimeError("«один файл» у цій грі не спрацював жодним варіантом — вибери інший спосіб")
            log("«один файл» не спрацював — ставлю OptiScaler")
    game = load_game(appid)
    if game.get("direct_files"):
        direct_remove(game)
        game.update(installed=False)
        save_game(appid, game)
    if game.get("bridge_files"):
        bridge_remove(game)
        game.update(installed=False)
        save_game(appid, game)
    if not installed_tag():
        download_latest(log)
    g = install(appid, install_path, game_name, args, settings, log, reset=reset,
                dll=load_game(appid).get("manual_dll"))
    g["method"] = "optiscaler"
    g["manual"] = manual
    save_game(appid, g)
    return g


def direct_feedback(appid, ok, log=print):
    """Відповідь користувача: чи був у кутку напис FSR 4. Ні → наступний варіант."""
    appid = str(appid)
    game = load_game(appid)
    if game.get("method") != "direct":
        return game
    if ok:
        game.update(direct_confirmed=True,
                    check={"t": int(time.time()), "status": "ok", "title": "✓ FSR 4 працює",
                           "message": f"Підтверджено: {game.get('upscaler_label', 'FSR 4')}"})
        save_game(appid, game)
        return game
    game["direct_tried"] = sorted(set(game.get("direct_tried", [])) |
                                  ({game["direct_variant"]} if game.get("direct_variant") else set()))
    game.pop("direct_variant", None)
    game["direct_runs"] = 0
    save_game(appid, game)
    try:
        return direct_install(appid, game.get("install"), game.get("name", ""), None, None, log)
    except DirectExhausted:
        if game.get("manual"):
            game["direct_tried"] = []
            save_game(appid, game)
            raise RuntimeError("усі варіанти «одного файлу» перепробувано — вибери інший спосіб повзунком")
        return enable(appid, game.get("install"), game.get("name", ""), None,
                      dict(C.load_json(C.SETTINGS_FILE, {}), opti_method="optiscaler"), log,
                      reset=False)


def uninstall(appid, log=print):
    appid = str(appid)
    game = load_game(appid)
    if not game:
        return {}
    if game.get("direct_files"):
        direct_remove(game)
    if game.get("bridge_files"):
        bridge_remove(game)
    _remove_files(game)
    game.update(installed=False, enabled=False, files=[], backups=[], updated=int(time.time()))
    save_game(appid, game)
    log(f"FSR 4 / OptiScaler прибрано з {game.get('target')}, оригінальні файли повернуто")
    return game


def ensure(appid, args, settings=None, log=print):
    """Перед запуском гри: перевстановити, якщо файли зникли (оновлення гри)
    або вийшла нова версія OptiScaler. Повертає (dll_stem | None, opti_fg)."""
    appid = str(appid)
    game = load_game(appid)
    if not game.get("enabled"):
        return None, False
    settings = settings or C.load_json(C.SETTINGS_FILE, {})
    if game.get("method") == "direct":
        return direct_ensure(appid, game, args, settings, log)
    if game.get("method") == "bridge":
        return bridge_ensure(appid, game, args, settings, log)
    target = game.get("target", "")
    missing = not os.path.isfile(os.path.join(target, game.get("dll", "dxgi.dll"))) or \
        not os.path.isfile(os.path.join(target, MARKER)) or \
        (game.get("ref") and not os.path.isfile(os.path.join(target, "dinput8.dll")))
    outdated = installed_tag() and game.get("tag") != installed_tag()
    gpu = effective_gpu(settings)
    want_up = game_upscaler(appid, settings, gpu)[0]
    want_var = fsr4_variant(gpu, want_up) if want_up in ("fsr4", "fsr40") else ""
    want_ratio = (ref_ratio(settings) or "auto") if game.get("ref") else ""
    changed = (game.get("upscaler"), game.get("opti_fg"), game.get("fsr4_variant", ""),
               game.get("ref_ratio", want_ratio)) != (
        want_up, bool(settings.get("opti_fg", False)), want_var, want_ratio)
    if missing or outdated or changed:
        why = "файли зникли (оновлення гри?)" if missing else \
            "нова версія OptiScaler" if outdated else "змінено налаштування"
        log(f"OptiScaler: перевстановлюю — {why}")
        try:
            if not installed_tag():
                download_latest(log)        # перехід з «одного файлу» на OptiScaler
            game = install(appid, game.get("install"), game.get("name", ""), args, settings, log)
        except Exception as e:  # noqa: BLE001
            game["error"] = str(e)
            save_game(appid, game)
            log(f"OptiScaler: не вдалося — {e}; гра запуститься без нього")
            return None, False
    dll = game.get("dll", "dxgi.dll")
    stem = None if dll.endswith(".asi") else os.path.splitext(dll)[0]
    if game.get("ref") and stem:
        stem = f"{stem},{REF_OVERRIDE}"     # обидві бібліотеки — з теки гри
    return stem, bool(game.get("opti_fg"))


WATERMARK_RUNS = 3     # скільки запусків показувати напис AMD «FSR 4», поки не підтверджено


def direct_ensure(appid, game, args, settings, log=print):
    """«Один файл»: файли на місці (після оновлення гри Steam міг їх стерти)?"""
    install = game.get("install") or ""
    missing = not game.get("direct_files") or any(
        not os.path.isfile(os.path.join(install, rel)) for rel in game.get("direct_files", []))
    gpu = effective_gpu(settings)
    stale = game.get("direct_variant") not in direct_chain(gpu)
    if missing or stale:
        why = ("попередній варіант не спрацював — пробую наступний" if not game.get("direct_variant")
               else "файли зникли (оновлення гри?)" if missing else "змінилась відеокарта")
        log(f"FSR 4: перевстановлюю «один файл» — {why}")
        try:
            game = direct_install(appid, install, game.get("name", ""), args, settings, log)
        except DirectExhausted:
            game = load_game(appid)
            game.update(method="optiscaler", installed=False)
            save_game(appid, game)
            return ensure(appid, args, settings, log)
        except Exception as e:  # noqa: BLE001
            game["error"] = str(e)
            save_game(appid, game)
            log(f"FSR 4: не вдалося — {e}; гра запуститься без нього")
            return None, False
    return (game.get("override") or None), False


def launch_extras(appid):
    """Що ще треба обгортці: спосіб і змінні середовища для цього запуску."""
    game = load_game(str(appid))
    if not game.get("enabled"):
        return "", {}
    method = game.get("method") or "optiscaler"
    env = {}
    if method == "direct" and not game.get("direct_confirmed") \
            and int(game.get("direct_runs", 0)) < WATERMARK_RUNS:
        env["FSR_WATERMARK"] = "1"     # бібліотека AMD сама пише в кутку, яка модель FSR 4 працює
    return method, env


# ------------------------------------------------------------------ самоперевірка

# порядок назв підмінної бібліотеки (вікі OptiScaler: dxgi → … → d3d12, якщо DLSS падає)
DLL_CHAIN = ["dxgi.dll", "winmm.dll", "version.dll", "d3d12.dll"]
MIN_RUN_FOR_VERDICT = 60      # коротша сесія без журналу — не вердикт
CRASH_SECONDS = 20


def _read_log(target, started):
    path = os.path.join(target or "", "OptiScaler.log")
    try:
        if os.path.getmtime(path) < started - 5:
            return ""          # журнал від попередньої сесії
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()[-400_000:]
    except OSError:
        return ""


def parse_log(text):
    """Що OptiScaler написав про цю сесію (рядки з його вихідного коду)."""
    working = re.findall(r"OptiScaler working as ([\w.\-]+)", text)
    created = re.findall(r"Creating new (\w+) upscaler", text)
    srver = re.findall(r"FfxApi Dx12 SR version: (\d+\.\d+\.\d+)", text)
    errors = [ln.strip()[-160:] for ln in text.splitlines()
              if re.search(r"Upscaler can't created|_createContext error|InitFSR\w* fail", ln)]
    return {
        "loaded": bool(working) or bool(re.search(r"OptiScaler v[\w.\-]+ loaded", text)),
        "proxy": working[-1] if working else "",
        "wine": "Running on Wine" in text,
        "upscalers": sorted(set(created)),
        "fsr_version": srver[-1] if srver else "",
        "errors": errors[-3:],
    }


def _next_dll(game):
    tried = set(game.get("dll_tried", [])) | {game.get("dll", "dxgi.dll")}
    for name in DLL_CHAIN:
        if name not in tried:
            return name
    return None


def analyze(appid, started, duration, crashed=False, log=print):
    """Після виходу з гри: чи OptiScaler працював. Перемикає назву DLL або вимикає.
    Повертає {"status", "title", "message"} або None, якщо OptiScaler у грі не ввімкнено."""
    appid = str(appid)
    game = load_game(appid)
    if not game.get("enabled"):
        return None
    if game.get("method") == "direct":
        return analyze_direct(appid, game, duration, crashed, log)
    if game.get("method") == "bridge":
        return analyze_bridge(appid, game, started, duration, crashed, log)
    info = parse_log(_read_log(game.get("target"), started))
    dll = game.get("dll", "dxgi.dll")
    res = {"t": int(time.time()), "duration": int(duration), "dll": dll, **info}

    def switch(reason):
        if game.get("manual"):
            # спосіб і бібліотеку вибрав користувач — нічого не перемикаємо самі
            return f"{reason}. Вибери іншу бібліотеку повзунком «Бібліотека OptiScaler» у меню AutoFPS"
        nxt = _next_dll(game)
        tried = sorted(set(game.get("dll_tried", [])) | {dll})
        game["dll_tried"] = tried
        if nxt:
            game["dll"] = nxt          # ensure() перевстановить під новою назвою
            res["next_dll"] = nxt
            return (f"{reason}. Наступного разу спробую назву {nxt} "
                    f"(спроба {len(tried) + 1} з {len(DLL_CHAIN)})")
        names = ", ".join(os.path.splitext(n)[0] for n in DLL_CHAIN)
        game["disabled_reason"] = f"Не спрацювала жодна назва бібліотеки ({names})"
        save_game(appid, game)
        uninstall(appid, log)
        game.update(load_game(appid))
        return (f"{reason}. Перепробувано всі назви бібліотеки ({names}) — OptiScaler для цієї гри "
                "вимкнено, оригінальні файли повернуто")

    if crashed or duration < CRASH_SECONDS and not info["loaded"]:
        res["status"] = "crash"
        title = "DLSS: гра закрилась одразу"
        msg = switch(f"Гра закрилась через {int(duration)} с з OptiScaler ({dll})")
    elif not info["loaded"] and game.get("dll_ok") == dll:
        # з цією ж назвою OptiScaler уже працював — журнал міг просто не записатись;
        # не перемикаємо назву через одну сесію без журналу
        res["status"] = "unknown"
        title = "DLSS: журнал OptiScaler не знайдено"
        msg = (f"Цього разу OptiScaler не залишив журналу, хоча з {dll} уже працював. "
               "Нічого не змінюю — перевірю наступного разу")
    elif not info["loaded"] and duration >= MIN_RUN_FOR_VERDICT:
        res["status"] = "not_loaded"
        title = "DLSS: OptiScaler не завантажився"
        msg = switch(f"Гра не підхопила {dll}")
    elif not info["loaded"]:
        res["status"] = "short"
        title = "DLSS: замало часу для перевірки"
        msg = "Пограй хоча б хвилину — тоді плагін перевірить, чи працює OptiScaler"
    elif info["upscalers"] and not info["errors"]:
        res["status"] = "ok"
        game["dll_ok"] = dll
        lbl = game.get("upscaler_label", "FSR")
        fsr = f" (FSR {info['fsr_version']})" if info["fsr_version"] and info["fsr_version"] not in lbl else ""
        title = "✓ DLSS працює"
        msg = (f"OptiScaler як {info['proxy'] or dll}, гра використовує DLSS → "
               f"{lbl}{fsr}")
    elif info["errors"]:
        res["status"] = "error"
        title = "DLSS: помилка масштабування"
        msg = (f"OptiScaler завантажився, але не зміг створити {game.get('upscaler_label', 'FSR')}: "
               f"{info['errors'][-1][:120]}. Спробуй «FSR 4.0» або «FSR 3.1»")
    elif game.get("ref"):
        res["status"] = "no_dlss"
        game["dll_ok"] = dll
        title = "FSR: увімкни TemporalUpscaler у REFramework"
        msg = ("OptiScaler і REFramework завантажились, але масштабування не ввімкнено. У грі відкрий "
               "меню REFramework (Insert або L3+R3) → TemporalUpscaler → увімкни. Це потрібно "
               "один раз — REFramework запам'ятає")
    else:
        res["status"] = "no_dlss"
        game["dll_ok"] = dll
        title = "DLSS: OptiScaler готовий, але DLSS не вибрано"
        msg = ("OptiScaler завантажився, але гра не запитувала DLSS. "
               "Вибери DLSS у налаштуваннях графіки гри")
    res.update(title=title, message=msg)
    game["check"] = res
    save_game(appid, game)
    log(f"самоперевірка {appid}: {res['status']} — {msg}")
    return res


def analyze_direct(appid, game, duration, crashed, log=print):
    """«Один файл» не пише журналу — перевіряємо падіння і просимо глянути на напис AMD."""
    v = game.get("direct_variant")
    res = {"t": int(time.time()), "duration": int(duration), "method": "direct", "variant": v}
    if crashed or duration < CRASH_SECONDS:
        if v:
            game["direct_tried"] = sorted(set(game.get("direct_tried", [])) | {v})
        else:
            v = (game.get("direct_tried") or ["valve"])[-1]
        game.pop("direct_variant", None)
        game["direct_runs"] = 0
        nxt = next((x for x in direct_chain(effective_gpu(C.load_json(C.SETTINGS_FILE, {}))) if x not in game["direct_tried"]), None)
        if game.get("manual") and not nxt:
            game["direct_tried"] = []           # вибір користувача: не переходимо на OptiScaler самі
            nxt = next(iter(direct_chain(effective_gpu(C.load_json(C.SETTINGS_FILE, {})))), None)
        res.update(status="crash", title="FSR 4: гра закрилась одразу",
                   message=(f"Гра закрилась через {int(duration)} с з «{DIRECT_VARIANTS[v]['label']}». "
                            + (f"Наступного разу спробую «{DIRECT_VARIANTS[nxt]['label']}»" if nxt
                               else "Варіанти «одного файлу» вичерпано — наступного разу буде OptiScaler")
                            + (". Не допомагає — вибери інший спосіб повзунком" if game.get("manual") else "")))
        if not nxt and not game.get("manual"):
            direct_remove(game)
            game.update(method="optiscaler", installed=False, direct_exhausted=True)
        game["check"] = res
        save_game(appid, game)
        log(f"самоперевірка {appid}: {res['message']}")
        return res
    game["direct_runs"] = int(game.get("direct_runs", 0)) + 1
    if game.get("direct_confirmed"):
        res.update(status="ok", title="✓ FSR 4 працює",
                   message=f"{game.get('upscaler_label')} — вибери FSR у налаштуваннях графіки гри")
        game["check"] = res
        save_game(appid, game)
        return None                     # підтверджено раніше — без зайвих сповіщень
    if duration < MIN_RUN_FOR_VERDICT:
        res.update(status="short", title="FSR 4: замало часу для перевірки",
                   message="Пограй хоча б хвилину з увімкненим FSR у налаштуваннях гри")
    else:
        res.update(status="verify", title="FSR 4: чи був напис у кутку?",
                   message=("Якщо в грі з увімкненим FSR у кутку був напис AMD «FSR 4» — натисни "
                            "в AutoFPS «Напис був ✓», і він зникне. Не було — «Не було», і я "
                            "спробую інший варіант"))
    game["check"] = res
    save_game(appid, game)
    log(f"самоперевірка {appid}: {res['status']}")
    return res


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "ensure":
        try:
            stem, fg = ensure(sys.argv[2], sys.argv[3:], log=lambda m: print(m, file=sys.stderr))
            method, env = launch_extras(sys.argv[2])
        except Exception as e:  # noqa: BLE001
            print(f"OptiScaler error: {e!r}", file=sys.stderr)
            stem, fg, method, env = None, False, "", {}
        print(f"DLL={stem or ''}")
        print(f"FG={1 if fg else 0}")
        print(f"METHOD={method if (stem or method in ('direct', 'bridge')) else ''}")
        for k, val in env.items():
            print(f"ENV={k}={val}")


if __name__ == "__main__":
    main()
