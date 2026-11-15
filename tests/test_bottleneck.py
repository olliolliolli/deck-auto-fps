"""Упор у процесор чи відеокарту — і що знижувати в кожному випадку."""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "defaults", "bin"))
import autotune as A  # noqa: E402
import generic_settings as G  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")


class Bottleneck(unittest.TestCase):
    def test_detect(self):
        e = {}
        self.assertEqual(A.bottleneck(e, {"p10": 40, "gpu": 98}, 50), "gpu")
        self.assertEqual(A.bottleneck(e, {"p10": 40, "gpu": 60}, 50), "cpu")
        self.assertEqual(e["gpu_load"], 60)
        # GPU недовантажена, але ціль досягнута — це не упор, лишаємо як було
        self.assertEqual(A.bottleneck(e, {"p10": 55, "gpu": 60}, 50), "cpu")
        self.assertEqual(A.bottleneck({}, {"p10": 55, "gpu": 60}, 50), None)
        # між порогами — без змін (гістерезис)
        self.assertEqual(A.bottleneck({"bound": "gpu"}, {"p10": 40, "gpu": 85}, 50), "gpu")
        self.assertIsNone(A.bottleneck({}, {"p10": 40}, 50))

    def test_ue_cpu_ladder_keeps_gpu_settings(self):
        L = A.ue_ladder(bound="cpu")
        self.assertEqual(len(L), 12)
        for st in L:
            for k in ("sg.ResolutionQuality", "sg.GlobalIlluminationQuality", "sg.ReflectionQuality",
                      "sg.PostProcessQuality", "sg.TextureQuality", "sg.ShadingQuality"):
                self.assertEqual(st[k], L[0][k], k)
        self.assertEqual(L[-1]["sg.ViewDistanceQuality"], "0")
        self.assertEqual(L[-1]["sg.FoliageQuality"], "0")
        self.assertEqual(L[1]["sg.ViewDistanceQuality"], "2")        # першим — дальність промальовки

    def test_ue_gpu_ladder_unchanged(self):
        self.assertEqual(A.ue_ladder(), A.ue_ladder(bound="gpu"))

    def test_unity_cpu(self):
        L = A.unity_ladder(4, bound="cpu")
        self.assertEqual([st["quality"] for st in L], [4, 3, 2, 1, 0])
        self.assertEqual({st["scale"] for st in L}, {A.RES_SCALES[0]})
        self.assertGreater(len({st["scale"] for st in A.unity_ladder(4)}), 1)   # GPU-драбина як була

    def test_re_engine_cpu(self):
        a = A.REEngineAdapter("", "", {"id": "legion_go_s", "res": [1920, 1200]})
        a._ceiling = lambda o: {"AppMeshQuality": 2, "AppShadowQuality": 3, "AppVolumeLightQuality": 3,
                                "AppAmbientOcclusion": 2, A.RE_FSR2_KEY: 1}
        a.bound = "cpu"
        L = a.ladder({})
        for st in L:
            self.assertEqual(st["AppVolumeLightQuality"], "3")
            self.assertEqual(st["AppAmbientOcclusion"], "2")
            self.assertEqual(st[A.RE_FSR2_KEY], "1")
        self.assertEqual(L[-1]["AppMeshQuality"], "0")
        self.assertEqual(L[-1]["AppShadowQuality"], "0")

    def test_generic_cpu_mode(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "UserSettings.json")
            shutil.copy(os.path.join(FIX, "cyberpunk_UserSettings.json"), p)
            a = G.GenericAdapter("", "", {}, G.FORMATS["json"], p)
            orig = a.read(p)
            a.bound = "cpu"
            ladder = a.ladder(orig)
            changed = {k for st in ladder for k in st if st[k] != orig[k]}
            self.assertTrue(changed)
            self.assertTrue(all(a._cpu_heavy(k) for k in changed), changed)
            self.assertFalse(any("Reflection" in k or "AmbientOcclusion" in k for k in changed))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
