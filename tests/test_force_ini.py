"""Unreal: гра тримає графіку у власному сейві — sg.* в Engine.ini [SystemSettings]."""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "defaults", "bin"))
import autotune as A  # noqa: E402


class ForceIni(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.gus = os.path.join(self.tmp, "GameUserSettings.ini")
        self.eng = os.path.join(self.tmp, "Engine.ini")
        with open(self.gus, "w") as f:
            f.write("[ScalabilityGroups]\nsg.ShadowQuality=1\nsg.GlobalIlluminationQuality=2\n")
        with open(self.eng, "w") as f:
            f.write("[SystemSettings]\nr.Foo=1\n\n[Core.Log]\nx=1\n")
        self.a = A.UnrealAdapter("", "", {"id": "legion_go_s", "res": [1920, 1200]})
        self.a.cfg = self.gus
        self.orig = {"sg.ShadowQuality": "1", "sg.GlobalIlluminationQuality": "2"}
        self.a.entry = {"force_ini": True, "original": self.orig}

    def tearDown(self):
        for root, _d, files in os.walk(self.tmp):
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_engine_ini_read_only(self):
        self.a.write(self.gus, {"sg.ShadowQuality": "0", "sg.GlobalIlluminationQuality": "0"})
        self.assertFalse(os.access(self.eng, os.W_OK))
        self.a.write(self.gus, {"sg.ShadowQuality": "1", "sg.GlobalIlluminationQuality": "0"})
        self.assertIn("sg.ShadowQuality=1", self.eng_text())     # наш повторний запис проходить

    def eng_text(self):
        with open(self.eng) as f:
            return f.read()

    def test_written_and_removed(self):
        vals = {"sg.ShadowQuality": "0", "sg.GlobalIlluminationQuality": "0"}
        self.a.write(self.gus, vals)
        t = self.eng_text()
        self.assertIn("sg.ShadowQuality=0", t)
        self.assertIn("r.Foo=1", t)
        self.assertEqual(t.count("[SystemSettings]"), 1)
        self.a.write(self.gus, vals)                        # повторно — без дублювання
        self.assertEqual(self.eng_text().count("sg.ShadowQuality"), 1)
        self.a.write(self.gus, self.orig)                   # відновлення — блок прибрано
        self.assertNotIn("AutoFPS graphics", self.eng_text())
        self.assertIn("r.Foo=1", self.eng_text())

    def test_cvars_written(self):
        self.a.write(self.gus, {"sg.ShadowQuality": "0", "sg.GlobalIlluminationQuality": "0"})
        t = self.eng_text()
        self.assertIn("r.Shadow.MaxResolution=512", t)
        self.assertIn("r.Lumen.TraceMeshSDFs=0", t)
        self.assertEqual(A.ue_cvars({"sg.ShadowQuality": "3"})["r.Shadow.CSM.MaxCascades"], "10")

    def test_off_without_flag(self):
        self.a.entry = {"original": self.orig}
        self.a.write(self.gus, {"sg.ShadowQuality": "0"})
        self.assertNotIn("sg.ShadowQuality", self.eng_text())


if __name__ == "__main__":
    unittest.main()
