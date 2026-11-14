#!/usr/bin/env python3
"""AutoFPS · звук на телевізор: при підключенні екрана — вихід HDMI/DP, при відключенні — назад.

Працює через pactl (PipeWire). Попередній вихід зберігається й повертається; звук, що вже
грає (гра), переноситься на новий вихід одразу.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofps_common as C  # noqa: E402

STATE = os.path.join(C.CFG_DIR, "dock_audio.json")


def _env():
    env = dict(os.environ)
    if hasattr(os, "getuid"):
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def _pactl(*args, run=None):
    run = run or subprocess.run
    r = run(["pactl", *args], env=_env(), capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise RuntimeError(f"pactl {' '.join(args)}: {(r.stderr or '').strip()[:200]}")
    return r.stdout


def sinks(run=None):
    return json.loads(_pactl("-f", "json", "list", "sinks", run=run) or "[]")


def tv_sink(lst):
    """Вихід HDMI/DisplayPort, на якому зараз справді є підключений пристрій."""
    for s in lst:
        name = s.get("name", "")
        if "hdmi" not in name.lower():
            continue
        ports = {p.get("name"): p.get("availability", "") for p in s.get("ports", [])}
        if ports.get(s.get("active_port")) == "available":
            return name
    return None


def _switch(target, run=None):
    _pactl("set-default-sink", target, run=run)
    for line in _pactl("list", "short", "sink-inputs", run=run).splitlines():
        idx = line.split("\t", 1)[0].strip()
        if idx.isdigit():
            try:
                _pactl("move-sink-input", idx, target, run=run)
            except RuntimeError:
                pass


def to_tv(run=None):
    """Перемкнути звук на телевізор. Повертає назву виходу або None."""
    tv = tv_sink(sinks(run))
    if not tv:
        return None
    cur = _pactl("get-default-sink", run=run).strip()
    if cur != tv:
        C.save_json(STATE, {"prev": cur})
        _switch(tv, run)
    return tv


def to_handheld(run=None):
    """Повернути звук на консоль (вихід, що був до телевізора). Повертає назву або None."""
    prev = C.load_json(STATE, {}).get("prev")
    names = [s.get("name") for s in sinks(run)]
    if not prev or prev not in names:
        prev = next((n for n in names if n and "hdmi" not in n.lower()), None)
    if not prev:
        return None
    if _pactl("get-default-sink", run=run).strip() != prev:
        _switch(prev, run)
    try:
        os.remove(STATE)
    except OSError:
        pass
    return prev


if __name__ == "__main__":
    print(to_tv() if len(sys.argv) > 1 and sys.argv[1] == "tv" else to_handheld())
