#!/usr/bin/env python3
"""AutoFPS · стеження за версіями: чи не стало гірше після оновлення плагіна.

Після кожної гри плагін записує кількість проблем самоперевірки для поточної версії.
Якщо в новій версії проблем (рівня error) більше, ніж було в попередній, — повертає
повідомлення з посиланням на попередню збірку для відкату.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofps_common as C  # noqa: E402

FILE = os.path.join(C.CFG_DIR, "version_health.json")
RELEASES = "https://github.com/olliolliolli/deck-auto-fps/releases"


def record(version, items):
    """Записати результат самоперевірки; повернути текст попередження або None."""
    errors = sorted({i["id"] for i in items if i.get("level") == "error"})
    data = C.load_json(FILE, {"order": [], "v": {}})
    if version not in data["v"]:
        data["order"].append(version)
        data["order"] = data["order"][-5:]
    data["v"][version] = {"errors": errors}
    data["v"] = {k: v for k, v in data["v"].items() if k in data["order"]}
    msg = None
    order = data["order"]
    if len(order) >= 2 and order[-1] == version:
        prev = order[-2]
        new = sorted(set(errors) - set(data["v"].get(prev, {}).get("errors", [])))
        if new and data.get("warned") != version:
            data["warned"] = version
            msg = (f"Після оновлення до {version} з'явились проблеми ({', '.join(new)}), "
                   f"яких не було у {prev}. Спробуй «Виправити» в самоперевірці; якщо не допоможе — "
                   f"встанови попередню збірку: {RELEASES}")
    C.save_json(FILE, data)
    return msg
