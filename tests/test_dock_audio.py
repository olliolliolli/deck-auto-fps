"""Звук на телевізор: перемикання й повернення (pactl підмінено)."""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "defaults", "bin"))
import dock_audio as DA  # noqa: E402

SINKS = [
    {"name": "GO S EQ", "ports": []},
    {"name": "alsa_output.Speaker__sink", "active_port": "[Out] Speaker",
     "ports": [{"name": "[Out] Speaker", "availability": "availability unknown"}]},
    {"name": "alsa_output.HiFi__HDMI3__sink", "active_port": "[Out] HDMI3",
     "ports": [{"name": "[Out] HDMI3", "availability": "not available"}]},
    {"name": "alsa_output.HiFi__HDMI4__sink", "active_port": "[Out] HDMI4",
     "ports": [{"name": "[Out] HDMI4", "availability": "available"}]},
]


class DockAudio(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = DA.STATE
        DA.STATE = os.path.join(self.tmp, "dock_audio.json")
        self.default = "GO S EQ"
        self.sinks = list(SINKS)
        self.calls = []

    def tearDown(self):
        DA.STATE = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake(self, cmd, **k):
        a = cmd[1:]
        self.calls.append(a)
        out = ""
        if a[:3] == ["-f", "json", "list"]:
            out = json.dumps(self.sinks)
        elif a == ["get-default-sink"]:
            out = self.default + "\n"
        elif a[0] == "set-default-sink":
            self.default = a[1]
        elif a[:3] == ["list", "short", "sink-inputs"]:
            out = "77\t44\tPipeWire\n"
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    def test_roundtrip(self):
        self.assertEqual(DA.to_tv(self.fake), "alsa_output.HiFi__HDMI4__sink")
        self.assertEqual(self.default, "alsa_output.HiFi__HDMI4__sink")
        self.assertIn(["move-sink-input", "77", "alsa_output.HiFi__HDMI4__sink"], self.calls)
        self.assertEqual(DA.to_handheld(self.fake), "GO S EQ")
        self.assertEqual(self.default, "GO S EQ")

    def test_no_tv_sink(self):
        self.sinks = [x for x in SINKS if "HDMI4" not in x["name"]]
        self.assertIsNone(DA.to_tv(self.fake))
        self.assertEqual(self.default, "GO S EQ")


if __name__ == "__main__":
    unittest.main()
