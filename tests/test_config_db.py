"""База шляхів до налаштувань (PCGamingWiki): пошук без урахування регістру, «*», вибір файлу."""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "defaults", "bin"))
import generic_settings as G  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")


class ConfigDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.compat = os.path.join(self.tmp, "compatdata", "777")
        self.user = os.path.join(self.compat, "pfx", "drive_c", "users", "steamuser")
        self.install = os.path.join(self.tmp, "common", "Game")
        os.makedirs(self.install)
        # реальна тека має інший регістр і ID користувача
        d = os.path.join(self.user, "AppData", "Local", "Studio", "MyGame", "76561198000000000")
        os.makedirs(d)
        shutil.copy(os.path.join(FIX, "cyberpunk_UserSettings.json"), os.path.join(d, "UserSettings.json"))
        with open(os.path.join(d, "keybinds.json"), "w") as f:
            json.dump({"jump": "space"}, f)
        self.db = os.path.join(self.tmp, "config_paths.json")
        with open(self.db, "w") as f:
            json.dump({"777": {"paths": ["pfx:AppData/Local/STUDIO/mygame/*", "game:config"]}}, f)
        self.old = G.CONFIG_DB
        G.CONFIG_DB = self.db
        G._db_cache.clear()

    def tearDown(self):
        G.CONFIG_DB = self.old
        G._db_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ci_glob(self):
        hits = G.ci_glob(self.user, "appdata/local/STUDIO/MyGame/*")
        self.assertEqual(len(hits), 1)

    def test_discover_from_db(self):
        fmt, path = G.discover(self.compat, self.install)
        self.assertEqual(fmt, "json")
        self.assertTrue(path.endswith("UserSettings.json"))

    def test_unknown_game_falls_back(self):
        G._db_cache["db"] = {}
        # без бази звичайний пошук теж знаходить (назва файлу підходить)
        self.assertIsNotNone(G.discover(self.compat, self.install))


if __name__ == "__main__":
    unittest.main()
