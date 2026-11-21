"""Факти PCGamingWiki: античит блокує OptiScaler, «лише для читання», дані в самоперевірці."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "defaults", "bin"))
import autofps_common as C  # noqa: E402


class Facts(unittest.TestCase):
    def test_loaded(self):
        f = C.game_facts("3287520")
        self.assertEqual(f.get("e"), "Unreal Engine 5")
        self.assertEqual(f.get("ro"), 1)
        self.assertIn("12", f.get("dx", []))
        self.assertTrue(C.game_facts("1172470").get("ac"))      # Apex Legends — античит
        self.assertEqual(C.game_facts("999999999"), {})


if __name__ == "__main__":
    unittest.main()
