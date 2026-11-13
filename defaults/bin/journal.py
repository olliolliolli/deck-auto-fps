#!/usr/bin/env python3
"""AutoFPS · журнал змін і автовідкат.

Кожен модуль, що змінює гру перед запуском (autotune, ue_tweaks, optiscaler…), обгортка
запускає через журнал:
    python3 journal.py run <appid> <скрипт.py> [аргументи…]
Журнал перехоплює запис файлів (open на запис, os.replace/rename/remove/unlink) і ПЕРЕД
першою зміною кожного файлу поза теками AutoFPS зберігає його оригінал (або позначку
«файлу не було»). Модулі для цього не змінюються.

Після гри обгортка викликає:
    python3 journal.py end <appid> <тривалість_с> <код> <поріг_швидкого_закриття_с>
Якщо гра закрилась швидше за поріг (найчастіше — впала на старті), усі зміни цього
запуску відкочуються, а в last_rollback.json пишеться звіт для сповіщення плагіна.
Інакше зміни підтверджуються. Зберігаються лише кілька останніх сесій.

Сесію задає змінна AUTOFPS_SESSION (обгортка ставить PID+час); без неї — appid.
"""
import builtins
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofps_common as C  # noqa: E402

ROOT = os.path.join(C.CACHE_DIR, "journal")
MAX_COPY = 64 * 1024 * 1024        # більші файли не копіюємо (позначаємо «nosnap»)
KEEP_SESSIONS = 3
WRITE_MODES = set("wax+")

_orig_open = builtins.open
_orig = {n: getattr(os, n) for n in ("replace", "rename", "remove", "unlink")}
_state = {"dir": None, "seen": set(), "busy": False}


def _session_dir(appid, session):
    safe = "".join(ch for ch in str(session) if ch.isalnum() or ch in "-_") or "s"
    return os.path.join(ROOT, str(appid), safe)


def _ours(path):
    """Файли самого AutoFPS (конфіги, кеш, журнал) не журналюються."""
    p = os.path.realpath(path)
    for d in (C.CFG_DIR, C.CACHE_DIR):
        d = os.path.realpath(d)
        if p == d or p.startswith(d + os.sep):
            return True
    return p.endswith((".autofps.tmp", ".tmp"))


def _manifest(sdir):
    return os.path.join(sdir, "manifest.json")


def _load(sdir):
    return C.load_json(_manifest(sdir), {"entries": [], "status": "pending", "t": int(time.time())})


def _save(sdir, data):
    _state["busy"] = True
    try:
        C.save_json(_manifest(sdir), data)
    finally:
        _state["busy"] = False


def snapshot(path):
    """Зберегти оригінал файлу перед першою зміною в цій сесії."""
    sdir = _state["dir"]
    if not sdir or _state["busy"] or not isinstance(path, (str, bytes, os.PathLike)):
        return
    path = os.fsdecode(path)
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    real = os.path.realpath(path)
    if real in _state["seen"] or _ours(real):
        return
    _state["seen"].add(real)
    _state["busy"] = True
    try:
        data = _load(sdir)
        if any(e["path"] == real for e in data["entries"]):
            return
        e = {"path": real, "t": int(time.time())}
        if not os.path.exists(real):
            e["kind"] = "absent"
        elif os.path.isfile(real) and os.path.getsize(real) <= MAX_COPY:
            os.makedirs(sdir, exist_ok=True)
            copy = os.path.join(sdir, f"{len(data['entries']):04d}.orig")
            shutil.copy2(real, copy)
            e.update(kind="file", copy=copy)
        else:
            e["kind"] = "nosnap"
        data["entries"].append(e)
        C.save_json(_manifest(sdir), data)
    except Exception as ex:  # noqa: BLE001 — журнал ніколи не заважає модулю
        sys.stderr.write(f"journal: snapshot {path}: {ex!r}\n")
    finally:
        _state["busy"] = False


def _open(file, mode="r", *a, **k):
    if isinstance(mode, str) and WRITE_MODES & set(mode) and not isinstance(file, int):
        snapshot(file)
    return _orig_open(file, mode, *a, **k)


def _wrap(name, arg_index):
    fn = _orig[name]

    def inner(*a, **k):
        if len(a) > arg_index:
            snapshot(a[arg_index])
        return fn(*a, **k)
    return inner


def install(appid, session=None):
    session = session or os.environ.get("AUTOFPS_SESSION") or str(appid)
    _state["dir"] = _session_dir(appid, session)
    _state["seen"] = set()
    builtins.open = _open
    os.replace, os.rename = _wrap("replace", 1), _wrap("rename", 1)
    os.remove, os.unlink = _wrap("remove", 0), _wrap("unlink", 0)


def uninstall():
    builtins.open = _orig_open
    for n, fn in _orig.items():
        setattr(os, n, fn)
    _state["dir"] = None


def rollback(sdir):
    """Повернути всі файли сесії до оригіналу. Повертає список відновлених шляхів."""
    data = _load(sdir)
    done = []
    for e in reversed(data["entries"]):
        p = e["path"]
        try:
            if e["kind"] == "file" and os.path.isfile(e.get("copy", "")):
                if os.path.isdir(p):
                    continue
                os.makedirs(os.path.dirname(p), exist_ok=True)
                tmp = p + ".autofps.tmp"
                shutil.copy2(e["copy"], tmp)
                _orig["replace"](tmp, p)
                done.append(p)
            elif e["kind"] == "absent" and os.path.isfile(p):
                _orig["remove"](p)
                done.append(p)
        except OSError as ex:
            sys.stderr.write(f"journal: rollback {p}: {ex!r}\n")
    data["status"] = "rolled_back"
    C.save_json(_manifest(sdir), data)
    return done


def _prune(appid):
    base = os.path.join(ROOT, str(appid))
    if not os.path.isdir(base):
        return
    dirs = sorted((d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))),
                  key=lambda d: os.path.getmtime(os.path.join(base, d)), reverse=True)
    for d in dirs[KEEP_SESSIONS:]:
        shutil.rmtree(os.path.join(base, d), ignore_errors=True)


def end(appid, dur, rc, fast_exit, session=None):
    session = session or os.environ.get("AUTOFPS_SESSION") or str(appid)
    sdir = _session_dir(appid, session)
    if not os.path.isfile(_manifest(sdir)):
        _prune(appid)
        return "no-changes"
    data = _load(sdir)
    changed = [e for e in data["entries"] if e["kind"] != "nosnap"]
    if dur < fast_exit and changed:
        files = rollback(sdir)
        C.save_json(os.path.join(C.CACHE_DIR, "last_rollback.json"), {
            "appid": str(appid), "t": int(time.time()), "dur": dur, "rc": rc,
            "files": files})
        # підбір графіки має знати, що його зміни відкочено (інакше прийме їх за ручні)
        tune = C.load_json(C.TUNE_FILE, {})
        for k, v in tune.items():
            if k.startswith(f"{appid}:") and isinstance(v, dict) and v.get("applied_values"):
                v.pop("applied_values", None)
                v.pop("applied_level", None)
                v["note"] = "Гра закрилась одразу після змін AutoFPS — зміни відкочено"
        C.save_json(C.TUNE_FILE, tune)
        result = f"rolled-back {len(files)}"
    else:
        data["status"] = "confirmed"
        C.save_json(_manifest(sdir), data)
        result = f"confirmed {len(data['entries'])}"
    _prune(appid)
    return result


def main(argv):
    if len(argv) >= 3 and argv[0] == "run":
        appid, script = argv[1], argv[2]
        try:
            install(appid)
        except Exception as ex:  # noqa: BLE001
            sys.stderr.write(f"journal: install failed: {ex!r}\n")
        import runpy
        sys.argv = [script] + argv[3:]
        sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
        runpy.run_path(script, run_name="__main__")
        return 0
    if len(argv) >= 5 and argv[0] == "end":
        print("journal:", end(argv[1], int(argv[2]), int(argv[3]), int(argv[4])))
        return 0
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
