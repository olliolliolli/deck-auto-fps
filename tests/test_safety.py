"""Тести журналу змін, перевірки запису, версій і звіту."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "defaults", "bin")
sys.path.insert(0, BIN)

import autofps_common as C  # noqa: E402
import journal as J  # noqa: E402
import autotune as A  # noqa: E402
import versions as V  # noqa: E402
import doctor as D  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = (C.CFG_DIR, C.CACHE_DIR, C.TUNE_FILE, J.ROOT, V.FILE, D.WRAPPER_LOG, D.LAUNCHER)
        C.CFG_DIR = os.path.join(self.tmp, "cfg")
        C.CACHE_DIR = os.path.join(self.tmp, "cache")
        C.TUNE_FILE = os.path.join(C.CFG_DIR, "tune.json")
        J.ROOT = os.path.join(C.CACHE_DIR, "journal")
        V.FILE = os.path.join(C.CFG_DIR, "version_health.json")
        D.WRAPPER_LOG = os.path.join(C.CACHE_DIR, "wrapper.log")
        D.LAUNCHER = os.path.join(self.tmp, "launcher")
        os.makedirs(C.CFG_DIR)
        os.makedirs(C.CACHE_DIR)
        self.game = os.path.join(self.tmp, "game")
        os.makedirs(self.game)

    def tearDown(self):
        J.uninstall()
        (C.CFG_DIR, C.CACHE_DIR, C.TUNE_FILE, J.ROOT, V.FILE, D.WRAPPER_LOG, D.LAUNCHER) = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.game, name), "w") as f:
            f.write(text)

    def read(self, name):
        with open(os.path.join(self.game, name)) as f:
            return f.read()


class Journal(Tmp):
    def changes(self):
        """Імітація модуля: змінює файл, замінює через tmp, створює новий і видаляє ще один."""
        J.install("7", "s1")
        self.write("a.ini", "new")
        tmp = os.path.join(self.game, "b.ini.tmp")
        with open(tmp, "w") as f:
            f.write("new")
        os.replace(tmp, os.path.join(self.game, "b.ini"))
        self.write("c.ini", "created")
        os.remove(os.path.join(self.game, "d.ini"))
        C.save_json(os.path.join(C.CFG_DIR, "x.json"), {"ours": 1})   # власний файл — не журналюється
        J.uninstall()

    def prepare(self):
        for n in ("a.ini", "b.ini", "d.ini"):
            self.write(n, "orig")

    def test_crash_rolls_back_everything(self):
        self.prepare()
        C.save_json(C.TUNE_FILE, {"7:dev": {"applied_values": {"x": 1}, "applied_level": 2}})
        self.changes()
        self.assertTrue(J.end("7", 3, 1, 20, "s1").startswith("rolled-back"))
        self.assertEqual([self.read(n) for n in ("a.ini", "b.ini", "d.ini")], ["orig"] * 3)
        self.assertFalse(os.path.exists(os.path.join(self.game, "c.ini")))
        self.assertTrue(os.path.isfile(os.path.join(C.CFG_DIR, "x.json")))
        self.assertNotIn("applied_values", C.load_json(C.TUNE_FILE, {})["7:dev"])
        self.assertEqual(C.load_json(os.path.join(C.CACHE_DIR, "last_rollback.json"), {})["appid"], "7")

    def test_normal_session_keeps_changes(self):
        self.prepare()
        self.changes()
        self.assertTrue(J.end("7", 600, 0, 20, "s1").startswith("confirmed"))
        self.assertEqual(self.read("a.ini"), "new")

    def test_first_original_kept_on_repeated_writes(self):
        self.prepare()
        J.install("7", "s1")
        self.write("a.ini", "one")
        self.write("a.ini", "two")
        J.uninstall()
        J.end("7", 1, 1, 20, "s1")
        self.assertEqual(self.read("a.ini"), "orig")

    def test_runner_cli(self):
        self.write("a.ini", "orig")
        script = os.path.join(self.tmp, "mod.py")
        with open(script, "w") as f:
            f.write("import sys\nopen(sys.argv[1], 'w').write('changed')\n")
        env = dict(os.environ, HOME=self.tmp, AUTOFPS_SESSION="cli")
        target = os.path.join(self.game, "a.ini")
        subprocess.run([sys.executable, os.path.join(BIN, "journal.py"), "run", "9", script, target],
                       env=env, check=True)
        self.assertEqual(self.read("a.ini"), "changed")
        subprocess.run([sys.executable, os.path.join(BIN, "journal.py"), "end", "9", "2", "1", "20"],
                       env=env, check=True)
        self.assertEqual(self.read("a.ini"), "orig")

    def test_no_changes_no_rollback(self):
        self.assertEqual(J.end("8", 1, 1, 20, "none"), "no-changes")


class WriteCheck(unittest.TestCase):
    def test_mismatch_majority(self):
        self.assertFalse(A.write_mismatch({"a": "1", "b": "2"}, {"a": "1.0", "b": "2"}))
        self.assertFalse(A.write_mismatch({"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "2", "c": "9"}))
        self.assertTrue(A.write_mismatch({"a": "1", "b": "2"}, {"a": "5", "b": "6"}))
        self.assertTrue(A.write_mismatch({"a": "1"}, {}))
        self.assertFalse(A.write_mismatch({}, {}))


class Versions(Tmp):
    def test_regression_warned_once(self):
        ok = [{"id": "launcher", "level": "ok"}]
        bad = [{"id": "launcher", "level": "error"}]
        self.assertIsNone(V.record("1.0", ok))
        msg = V.record("1.1", bad)
        self.assertIn("1.0", msg)
        self.assertIsNone(V.record("1.1", bad))

    def test_no_warning_when_same(self):
        bad = [{"id": "mangohud", "level": "error"}]
        V.record("1.0", bad)
        self.assertIsNone(V.record("1.1", bad))


class Report(Tmp):
    def test_url_short_and_complete(self):
        with open(D.WRAPPER_LOG, "w", encoding="utf-8") as f:
            f.write("\n".join(f"[t] рядок журналу номер {i} з кирилицею" for i in range(500)))
        url = D.report("1091500", "legion_go_s")
        self.assertLessEqual(len(url), D.MAX_URL)
        body = urllib.parse.unquote(url.split("&body=", 1)[1])
        self.assertIn("Самоперевірка", body)
        self.assertIn("wrapper.log", body)


if __name__ == "__main__":
    unittest.main()
