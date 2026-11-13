"""Нативні порти Feral (Rise of the Tomb Raider на Linux)."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "defaults", "bin"))
import generic_settings as G  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "feral_rottr_preferences")


class Feral(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.prefs = os.path.join(self.home, ".local", "share", "feral-interactive",
                                  "Rise of the Tomb Raider", "preferences")
        os.makedirs(os.path.dirname(self.prefs))
        shutil.copy(FIX, self.prefs)
        self.install = os.path.join(self.tmp, "common", "Rise of the Tomb Raider")
        os.makedirs(self.install)
        self.compat = os.path.join(self.tmp, "compatdata", "391220")   # префікса немає — нативна гра
        self.p = mock.patch("os.path.expanduser",
                            side_effect=lambda x: x.replace("~", self.home, 1))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_settings_found(self):
        st = G.GenericAdapter._settings_of(G.FORMATS["feral"](self.prefs))
        names = {k.rsplit("\\", 1)[-1] for k in st}
        self.assertTrue({"ShadowQuality", "TextureQuality", "AmbientOcclusionQuality"} <= names)
        self.assertFalse(any("Default" in n for n in names))
        self.assertNotIn("FullscreenWidth", names)

    def test_discover_native(self):
        fmt, path = G.discover(self.compat, self.install)
        self.assertEqual((fmt, os.path.normpath(path)), ("feral", os.path.normpath(self.prefs)))

    def test_write_only_graphics(self):
        f = G.FORMATS["feral"](self.prefs)
        key = [k for k, _ in f.items() if k.endswith("ShadowQuality") and "Soft" not in k][0]
        with open(self.prefs, encoding="utf-8") as fh:
            before = fh.read()
        f.write({key: "0"})
        with open(self.prefs, encoding="utf-8") as fh:
            after = fh.read()
        self.assertEqual(dict(f.items())[key], "0")
        diff = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
        self.assertEqual(len(diff), 1)
        self.assertIn('name="ShadowQuality"', diff[0][1])
        self.assertIn('DefaultShadowQuality" type="integer">1<', after)

    def test_every_step_changes_something(self):
        a = G.GenericAdapter("", "", {}, G.FORMATS["feral"], self.prefs)
        ladder = a.ladder(a.read(self.prefs))
        self.assertEqual(ladder[0], a.read(self.prefs))
        for i in range(1, len(ladder)):
            self.assertNotEqual(ladder[i], ladder[i - 1], f"сходинка {i} без змін")
        self.assertGreaterEqual(len(ladder), 4)

    def test_other_game_not_matched(self):
        self.assertIsNone(G.discover(self.compat, os.path.join(self.tmp, "common", "Portal 2")))


if __name__ == "__main__":
    unittest.main()
