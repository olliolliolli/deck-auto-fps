"""Unreal: текстові рівні якості у власному розділі гри (Stellar Blade)."""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "defaults", "bin"))
import autotune as A  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "stellarblade_GameUserSettings.ini")


class Enum(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "GameUserSettings.ini")
        shutil.copy(FIX, self.cfg)
        self.a = A.UnrealAdapter("", "", {"id": "legion_go_s", "res": [1920, 1200]})
        self.a.cfg = self.cfg

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def text(self):
        with open(self.cfg, encoding="utf-8") as f:
            return f.read()

    def test_levels(self):
        self.assertEqual(A.enum_level("ShadowQuality", "SB_GAMEUSERSETTINGS_VERYHIGH"), 3)
        self.assertEqual(A.enum_level("Lighting", "SB_GAMEUSERSETTINGS_MEDIUM"), 1)
        self.assertIsNone(A.enum_level("NvidiaDLSS", "SB_GAMEUSERSETTINGS_VERYHIGH"))
        self.assertIsNone(A.enum_level("NvidiaReflexLowLatency", "SB_GAMEUSERSETTINGS_LOW"))
        self.assertIsNone(A.enum_level("GrainQuality", "SB_GAMEUSERSETTINGS_OFF"))

    def test_write_in_game_words(self):
        cur = self.a.read(self.cfg)
        self.assertEqual(cur["x.ShadowQuality"], "3")
        self.a.extra_max = {k: v for k, v in cur.items() if k.startswith("x.")}
        st = self.a.ladder(cur)[8]
        self.a.write(self.cfg, st)
        t = self.text()
        self.assertIn("ShadowQuality=SB_GAMEUSERSETTINGS_MEDIUM", t)   # тіні не нижче 1
        self.assertIn("NvidiaDLSS=SB_GAMEUSERSETTINGS_VERYHIGH", t)           # апскейлер не чіпаємо
        self.assertIn("MaterialQuality=SB_GAMEUSERSETTINGS_LOW", t)           # не вище, ніж було
        self.assertNotIn("=SB_GAMEUSERSETTINGS_0", t)
        # і зворотно: прочитане = записане (перевірка запису не спрацює хибно)
        back = self.a.read(self.cfg)
        self.assertFalse(A.write_mismatch(self.a.comparable(st), back))


if __name__ == "__main__":
    unittest.main()
