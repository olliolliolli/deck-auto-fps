"""Автотести AutoFPS без пристрою: python3 -m unittest discover -s tests -v"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "defaults", "bin")
FIX = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, BIN)

import generic_settings as G  # noqa: E402
import autotune as A  # noqa: E402
import doctor as D  # noqa: E402
import autofps_common as C  # noqa: E402

CP_REL = ["AppData", "Local", "CD Projekt Red", "Cyberpunk 2077", "UserSettings.json"]


def user_dir(compat):
    return os.path.join(compat, "pfx", "drive_c", "users", "steamuser")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.compat = os.path.join(self.tmp, "compatdata", "1091500")
        self.install = os.path.join(self.tmp, "common", "Cyberpunk 2077")
        os.makedirs(user_dir(self.compat))
        os.makedirs(self.install)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put_settings(self):
        p = os.path.join(user_dir(self.compat), *CP_REL)
        os.makedirs(os.path.dirname(p))
        shutil.copy(os.path.join(FIX, "cyberpunk_UserSettings.json"), p)
        return p

    def put_decoy(self):
        """Файл у теці гри з більшою кількістю «налаштувань», ніж справжній."""
        d = os.path.join(self.install, "r6", "config")
        os.makedirs(d)
        names = ["Shadow", "Fog", "Reflection", "Cloud", "Water", "Hair", "Smoke", "Decal", "Light", "Effect"]
        data = {"data": [{"group_name": "/graphics/advanced", "options": [
            {"name": n + "Quality", "value": "High", "values": ["Low", "Medium", "High"]} for n in names]}]}
        with open(os.path.join(d, "options.json"), "w") as f:
            json.dump(data, f)


class GenericSettings(Base):
    def test_json_parses_cyberpunk(self):
        f = G.FORMATS["json"](os.path.join(FIX, "cyberpunk_UserSettings.json"))
        self.assertGreaterEqual(len(G.GenericAdapter._settings_of(f)), 5)

    def test_known_game_listed(self):
        self.assertEqual(G.known_game("1091500")["format"], "json")
        self.assertIsNone(G.known_game("999999999"))

    def test_known_file_beats_decoy(self):
        p = self.put_settings()
        self.put_decoy()
        self.assertEqual(os.path.normpath(G.discover(self.compat, self.install)[1]), os.path.normpath(p))

    def test_known_file_beats_cached_wrong_choice(self):
        p = self.put_settings()
        self.put_decoy()
        cached = ("json", os.path.join(self.install, "r6", "config", "options.json"))
        a = G.GenericAdapter.find(self.install, self.compat, {}, cached)
        self.assertEqual(os.path.normpath(a.cfg), os.path.normpath(p))

    def test_prefix_beats_install_without_known(self):
        compat = os.path.join(self.tmp, "compatdata", "123")
        p = os.path.join(user_dir(compat), "AppData", "Local", "Game", "UserSettings.json")
        os.makedirs(os.path.dirname(p))
        shutil.copy(os.path.join(FIX, "cyberpunk_UserSettings.json"), p)
        self.put_decoy()
        self.assertEqual(os.path.normpath(G.discover(compat, self.install)[1]), os.path.normpath(p))


class Reasons(Base):
    def test_settings_not_created(self):
        self.assertEqual(A._why_not_found("1091500", self.compat)[0], "settings_not_created")

    def test_no_prefix(self):
        self.assertEqual(A._why_not_found("5", os.path.join(self.tmp, "none"))[0], "native_unsupported")

    def test_unsupported(self):
        compat = os.path.join(self.tmp, "compatdata", "6")
        os.makedirs(os.path.join(compat, "pfx"))
        self.assertEqual(A._why_not_found("6", compat)[0], "unsupported")


class Doctor(Base):
    def setUp(self):
        super().setUp()
        self.old = (C.CFG_DIR, C.TUNE_FILE, D.LAUNCHER, D.WRAPPER_LOG)
        C.CFG_DIR = os.path.join(self.tmp, "cfg")
        C.TUNE_FILE = os.path.join(C.CFG_DIR, "tune.json")
        D.LAUNCHER = os.path.join(self.tmp, "launcher")
        D.WRAPPER_LOG = os.path.join(self.tmp, "wrapper.log")
        os.makedirs(C.CFG_DIR)

    def tearDown(self):
        C.CFG_DIR, C.TUNE_FILE, D.LAUNCHER, D.WRAPPER_LOG = self.old
        super().tearDown()

    def by_id(self, appid=None):
        return {i["id"]: i for i in D.check(appid)}

    def test_missing_launcher_fixable(self):
        it = self.by_id()["launcher"]
        self.assertEqual((it["level"], it["fix"]), ("error", "launcher"))
        made = []
        ok, _ = D.fix("launcher", lambda: made.append(1))
        self.assertTrue(ok and made)

    def test_safe_mode_reset(self):
        with open(os.path.join(C.CFG_DIR, "mode_42"), "w") as f:
            f.write("safe")
        it = self.by_id("42")["mode"]
        self.assertEqual(it["fix"], "reset_mode:42")
        D.fix(it["fix"])
        self.assertEqual(self.by_id("42")["mode"]["level"], "ok")

    def test_unknown_graphics_retry(self):
        C.save_json(C.TUNE_FILE, {"42:legion_go_s": {"engine": "unknown", "note": "x", "reason": "unsupported"}})
        it = self.by_id("42")["graphics"]
        self.assertEqual((it["level"], it["detail"]), ("warn", "x"))
        D.fix(it["fix"])
        self.assertNotIn("engine", C.load_json(C.TUNE_FILE, {})["42:legion_go_s"])

    def test_log_errors_reported(self):
        with open(D.WRAPPER_LOG, "w", encoding="utf-8") as f:
            f.write("[t] === Запуск гри appid=1\n[t] autotune завершився з помилкою (ігнорую)\n")
        self.assertEqual(self.by_id()["log"]["level"], "warn")


class Target(unittest.TestCase):
    def test_fg_halves_limit(self):
        st = {"quality_target": "auto", "bat_mode": "50", "last_fg_mult": 2}
        dev = {"quality_target": 40}
        self.assertEqual(C.quality_target(st, dev), 30)
        self.assertIn("×2", C.quality_target_why(st, dev))
        self.assertIn("30", C.quality_target_why(st, dev))
        st["last_fg_mult"] = 1
        self.assertEqual(C.quality_target(st, dev), 50)


if __name__ == "__main__":
    unittest.main()
