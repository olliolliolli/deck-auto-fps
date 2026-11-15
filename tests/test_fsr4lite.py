"""FSR 4.1 Lite: вибір, налаштування OptiScaler, перевірка за журналом."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "defaults", "bin"))
import optiscaler as O  # noqa: E402

GPU = {"vendor": "amd", "arch": "rdna3", "name": "Phoenix / Ryzen Z1 Extreme"}


class Lite(unittest.TestCase):
    def test_choice(self):
        key, label = O.resolve_upscaler("fsr4lite", GPU)
        self.assertEqual(key, "fsr4lite")
        self.assertIn("Lite", label)
        self.assertEqual(O.fsr4_variant(GPU, "fsr4lite"), "lite-412")
        self.assertEqual(O.fsr4_variant(GPU, "fsr4"), "rdna34-official-411")   # старий FSR 4 як був

    def test_ini(self):
        ov = O.ini_overrides("unreal", "1551980", "fsr4lite", False, "lite-412")
        self.assertEqual(ov["FSR.Fsr4Model"], "3")
        self.assertEqual(ov["FSR.Fsr4GpuSupport"], "4")
        self.assertEqual(ov["FSR.Fsr4Update"], "true")
        self.assertEqual(ov["Upscalers.Dx12Upscaler"], "ffx")

    def test_assets_pinned(self):
        for k in ("amdxcffx64_lite_412", "optiscaler_nightly_lite"):
            self.assertRegex(O.ASSETS[k]["sha256"], r"^[0-9a-f]{64}$")

    def test_log_lite(self):
        base = "OptiScaler v10.0.0 loaded\nOptiScaler working as dxgi.dll\nCreating new FFX upscaler\n"
        ok = base + ("Hooked ffxProvider_FSR4Lite_Int8\nLoaded amdxcffx64.dll\n"
                     "Render Resolution: 960x540, Display Resolution: 1920x1080\n")
        self.assertTrue(O.parse_log(ok)["lite"])
        v10 = ("OptiScaler working as dxgi.dll\nHooked the support check of ffxProvider_FSR4Lite_Int8\n"
               "amdxcffx64.dll loaded from X\nRender Resolution: 960x540, Display Resolution 1920x1080\n"
               "Created driver upscaler context, result: The operation was successful.\n")
        info = O.parse_log(v10)
        self.assertTrue(info["lite"] and info["upscalers"])
        self.assertFalse(O.parse_log(base + "Hooked ffxProvider_FSR4Lite_Int8\n")["lite"])
        self.assertFalse(O.parse_log(ok.replace("Loaded amdxcffx64.dll", "Failed to load amdxcffx64.dll"))["lite"])


if __name__ == "__main__":
    unittest.main()
