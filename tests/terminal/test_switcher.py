"""Tests for the in-terminal model switcher (strata_cli/switcher.py and monitor integration)."""

from __future__ import annotations

import os
import unittest
from strata_cli import switcher
from strata_cli.client import Endpoint
from strata_cli.monitor import MonitorApp


class TestSwitcher(unittest.TestCase):
    def test_get_server_pid_unused_port(self):
        # A high port that should have no listener
        pid = switcher.get_server_pid(59876)
        self.assertIsNone(pid)

    def test_get_active_server_status_stopped(self):
        status = switcher.get_active_server_status(59876)
        self.assertEqual(status.get("badge"), "STOPPED")
        self.assertIn("59876", status.get("detail", ""))

    def test_discover_models(self):
        models = switcher.discover_models()
        self.assertIsInstance(models, list)
        self.assertGreater(len(models), 0)
        
        # Verify our standard configs are discovered
        ids = [m["id"] for m in models]
        self.assertIn("strata-m5-64k.json", ids)
        self.assertIn("strata-swift-512k.json", ids)
        self.assertIn("strata-qwen-512k.json", ids)

        # Check label formatting
        m5 = next(m for m in models if m["id"] == "strata-m5-64k.json")
        self.assertIn("Daily", m5["label"])
        self.assertIn("64k", m5["label"])

        swift512 = next(m for m in models if m["id"] == "strata-swift-512k.json")
        self.assertIn("512k", swift512["label"])

    def test_model_switch_screen_options(self):
        models = [
            {"id": "m1", "label": "Model 1", "port": 8080},
            {"id": "m2", "label": "Model 2", "port": 8080},
        ]
        status = {"badge": "RUNNING", "detail": "Active model on port 8080"}
        screen = switcher.ModelSwitchScreen(status, models)

        self.assertEqual(len(screen.options), 4)  # 2 models + stop + cancel
        self.assertEqual(screen.options[0][0], "model:m1")
        self.assertTrue(screen.options[0][1].startswith("1) "))
        self.assertEqual(screen.options[1][0], "model:m2")
        self.assertTrue(screen.options[1][1].startswith("2) "))
        self.assertEqual(screen.options[2][0], "stop")
        self.assertEqual(screen.options[3][0], "cancel")

    def test_monitor_app_port_and_bindings(self):
        ep = Endpoint("http://127.0.0.1:8080")
        app = MonitorApp(ep, None)
        self.assertEqual(app._current_port(), 8080)

        # Verify 'm' and 's' bindings are present
        keys = [b.key for b in app.BINDINGS]
        self.assertIn("m", keys)
        self.assertIn("s", keys)


if __name__ == "__main__":
    unittest.main()
