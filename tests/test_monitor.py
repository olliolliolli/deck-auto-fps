"""Моніторинг: рішення за середнім FPS за хвилину, а не за найгіршою секундою."""
import logging
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "defaults", "bin"))

d = types.ModuleType("decky")
d.logger = logging.getLogger("t")
d.DECKY_USER_HOME = os.path.expanduser("~")
d.DECKY_PLUGIN_DIR = ROOT


async def _emit(*a, **k):
    pass


d.emit = _emit
sys.modules.setdefault("decky", d)
import main  # noqa: E402


class Monitor(unittest.TestCase):
    def setUp(self):
        p = main.Plugin.__new__(main.Plugin)
        p.base, p.mult, p.sfg = 50, 1, 1
        p.phase, p.phase_t = "locked", 0.0
        p.samples, p.bad_windows, p.idle_windows = [], 0, 0
        p.gpu_state, p.last_apply, p.last_probe = "off", 1e12, 0
        p.settings = {"drop_ratio": 0.88}
        p.device = {"tiers": [30, 40, 60, 120]}
        p.downs = []
        p._apply = lambda: None
        p._gpu_rescue = lambda *a: False
        p._tdp_rescue = lambda: False
        p._record_tune = lambda *a, **k: None
        p._step_down = lambda now, v: p.downs.append(round(v, 1))
        self.p = p
        self.t = 20.0

    def play(self, seconds, fps):
        """Подати семпли (4 за секунду) і викликати частину _tick для стану locked."""
        for _ in range(int(seconds)):
            for k in range(4):
                self.p.samples.append((self.t + k * 0.25, fps, 99))
            self.t += 1
            if int(self.t) % 10 == 0:
                main.Plugin._monitor_locked(self.p, self.t)

    def test_short_heavy_scene_ignored(self):
        for _ in range(5):              # 5 хв: 50 с легко, 10 с важко (35 FPS)
            self.play(50, 56)
            self.play(10, 35)
        self.assertEqual(self.p.downs, [])

    def test_sustained_drop_lowers(self):
        self.play(60, 55)
        self.play(70, 40)
        self.assertEqual(len(self.p.downs), 1)
        self.assertLess(self.p.downs[0], 44)


if __name__ == "__main__":
    unittest.main()
