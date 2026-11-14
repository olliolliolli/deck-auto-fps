"""Роздільність, збережена в самій грі, під поточний екран."""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "defaults", "bin"))
import res_fix as R  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")


def drm(tmp, tv=None):
    d = os.path.join(tmp, "drm")
    for name, st, mode in (("card0-eDP-1", "connected", "1920x1200"),
                           ("card0-DP-2", "connected" if tv else "disconnected", tv or "")):
        os.makedirs(os.path.join(d, name), exist_ok=True)
        with open(os.path.join(d, name, "status"), "w") as f:
            f.write(st)
        with open(os.path.join(d, name, "modes"), "w") as f:
            f.write(mode)
    return d


class ResFix(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.reg = os.path.join(self.tmp, "user.reg")
        shutil.copy(os.path.join(FIX, "tr2013_user.reg"), self.reg)
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read(self, p):
        with open(p, encoding="utf-8") as f:
            return f.read()

    def test_target(self):
        self.assertEqual(R.target_for(1920, 1080, (1920, 1200)), (1920, 1200))
        self.assertIsNone(R.target_for(1920, 1200, (1920, 1200)))
        self.assertEqual(R.target_for(1920, 1200, (3840, 2160)), (1920, 1080))
        self.assertEqual(R.target_for(1600, 900, (1920, 1200)), (1680, 1050))
        self.assertIsNone(R.target_for(1920, 1080, (1920, 1080)))

    def test_handheld_fixes_tv_resolution(self):
        R.fix_reg(self.reg, R.screen(drm(self.tmp)), log=self.logs.append)
        s = self.read(self.reg)
        graphics, wine = s.split("Display] 1")
        self.assertIn('"FullscreenWidth"=dword:00000780', graphics)
        self.assertIn('"FullscreenHeight"=dword:000004b0', graphics)          # 1200
        self.assertIn('"FullscreenHeight"=dword:00000438', wine)  # Wine не чіпаємо
        self.assertEqual(len(self.logs), 1)

    def test_tv_keeps_16_9(self):
        self.assertFalse(R.fix_reg(self.reg, R.screen(drm(self.tmp, "1920x1080\n1280x720")), log=self.logs.append))

    def test_session_tv_then_restore(self):
        compat = os.path.join(self.tmp, "compat")
        os.makedirs(os.path.join(compat, "pfx"))
        shutil.copy(os.path.join(FIX, "tr2013_user.reg"), os.path.join(compat, "pfx", "user.reg"))
        reg = os.path.join(compat, "pfx", "user.reg")
        d = drm(self.tmp, "1920x1080")
        R.run("203160", [], drm=d, compat=compat, log=self.logs.append)          # старт на ТВ
        self.assertIn('"FullscreenHeight"=dword:00000438', self.read(reg).split("Display] 1")[0])
        R.run("203160", [], drm=d, compat=compat, log=self.logs.append, restore=True)  # вихід
        self.assertIn('"FullscreenHeight"=dword:000004b0', self.read(reg).split("Display] 1")[0])

    def test_feral(self):
        p = os.path.join(self.tmp, "preferences")
        shutil.copy(os.path.join(FIX, "feral_rottr_preferences"), p)
        R.fix_feral(p, (3840, 2160), log=self.logs.append)
        s = self.read(p)
        self.assertIn('<value name="FullscreenHeight" type="integer">1080</value>', s)
        self.assertIn('<value name="FullscreenWidth" type="integer">1920</value>', s)
        self.assertNotIn("1200", "".join(l for l in s.splitlines() if 'name="FullscreenHeight"' in l))


if __name__ == "__main__":
    unittest.main()
