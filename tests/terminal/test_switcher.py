"""Tests for the in-terminal model switcher (strata_cli/switcher.py and monitor integration)."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock
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


class TestMonitorSwitcher(unittest.TestCase):
    def test_config_api_key_shapes(self):
        self.assertEqual(switcher.config_api_key({"data": {"api_key": "k1,k2"}}), "k1")
        self.assertEqual(switcher.config_api_key({"data": {"api_key": ["a", "b"]}}), "a")
        self.assertEqual(switcher.config_api_key({"data": {"api_key": "none"}}), "")
        self.assertEqual(switcher.config_api_key({"data": {}}), "")

    def test_switch_launches_on_monitor_port_and_adopts_key(self):
        """A config's own port/key must not leave the monitor watching 8095 while the model serves 8000."""
        app = MonitorApp(Endpoint("http://127.0.0.1:8095"), None)
        app._paint = lambda *a, **k: None
        app._schedule_poll = lambda *a, **k: None
        calls = {}

        def fake_stop(port):
            calls["stop"] = port
            return True

        def fake_launch(model, port):
            calls["launch"] = (model["id"], port)

        async def fake_ready(port, host="127.0.0.1", timeout=120.0, key=None):
            calls["ready"] = (port, host, key)
            return True

        model = {"id": "m1", "label": "Model 1", "port": 8000,
                 "model_name": "Model 1", "data": {"api_key": "secret"}}
        with mock.patch.object(switcher, "stop_server_on_port", fake_stop), \
                mock.patch.object(switcher, "launch_model", fake_launch), \
                mock.patch.object(switcher, "wait_for_server_ready", fake_ready):
            asyncio.run(app._perform_switch_model(model, 8095))

        self.assertEqual(calls["stop"], 8095)
        self.assertEqual(calls["launch"], ("m1", 8095))       # the monitor's port, not the config's 8000
        self.assertEqual(calls["ready"][0], 8095)
        self.assertEqual(calls["ready"][2], "secret")         # readiness polls carry the server's key
        self.assertEqual(app.endpoint.credential, "secret")   # telemetry will not 401
        self.assertFalse(app.switching)

    def test_switcher_offers_once_when_no_connection(self):
        app = MonitorApp(Endpoint("http://127.0.0.1:8095"), None)
        app.has_snapshot = False
        app.connection_state = "disconnected"
        self.assertTrue(app._should_offer_switcher())
        app.switcher_offered = True
        self.assertFalse(app._should_offer_switcher())
        app.switcher_offered = False
        app.connection_state = "connected"
        self.assertFalse(app._should_offer_switcher())
        app.connection_state = "disconnected"
        app.offer_switcher_on_disconnect = False
        self.assertFalse(app._should_offer_switcher())

    def test_remote_endpoint_does_not_offer_switcher(self):
        app = MonitorApp(Endpoint("https://example.com"), None)
        app.has_snapshot = False
        app.connection_state = "disconnected"
        self.assertFalse(app._should_offer_switcher())

    def test_second_m_does_not_stack_a_second_dialog(self):
        async def exercise():
            app = MonitorApp(Endpoint("http://127.0.0.1:59999"), None, interval=0.5)
            async with app.run_test(size=(100, 30)) as pilot:
                for _ in range(40):
                    if app.switcher_offered:
                        break
                    await pilot.pause(0.25)
                await pilot.press("m")
                await pilot.pause()
                return sum(isinstance(s, switcher.ModelSwitchScreen) for s in app.screen_stack)
        self.assertEqual(asyncio.run(exercise()), 1)

    def test_switch_is_ignored_while_a_launch_is_running(self):
        async def exercise():
            app = MonitorApp(Endpoint("http://127.0.0.1:59999"), None, interval=30)
            app.offer_switcher_on_disconnect = False   # nothing may open on its own in this test
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app.switching = True
                await pilot.press("m")
                await pilot.pause()
                return sum(isinstance(s, switcher.ModelSwitchScreen) for s in app.screen_stack)
        self.assertEqual(asyncio.run(exercise()), 0)


if __name__ == "__main__":
    unittest.main()
