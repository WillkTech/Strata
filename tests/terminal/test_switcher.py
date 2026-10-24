"""Tests for the in-terminal model switcher (strata_cli/switcher.py and monitor integration)."""

from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from strata_cli import switcher
from strata_cli import launch as _launch_package  # noqa: F401 - keeps strata_cli.launch importable
from strata_cli.launch import store as launch_store
from strata_cli.launch.monitor_ext import LaunchMonitor
from strata_cli.client import Endpoint
from strata_cli.monitor import MonitorApp
from textual.app import App


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

        self.assertEqual(len(screen.options), 5)  # 2 models + stop + port + cancel
        self.assertEqual(screen.options[0][0], "model:m1")
        self.assertTrue(screen.options[0][1].startswith("1) "))
        self.assertEqual(screen.options[1][0], "model:m2")
        self.assertTrue(screen.options[1][1].startswith("2) "))
        self.assertEqual(screen.options[2][0], "stop")
        self.assertEqual(screen.options[3][0], "port")
        self.assertEqual(screen.options[4][0], "cancel")

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


class TestDialogTitleAndColour(unittest.TestCase):
    def test_title_follows_the_action(self):
        self.assertEqual(switcher.dialog_title("connected"), (switcher.TITLE_SWITCH, "accent"))
        self.assertEqual(switcher.dialog_title("auth_required"), (switcher.TITLE_SWITCH, "warn"))
        self.assertEqual(switcher.dialog_title("disconnected"), (switcher.TITLE_LAUNCH, "bad"))
        self.assertEqual(switcher.dialog_title("connecting"), (switcher.TITLE_LAUNCH, "warn"))
        # a frozen display is not a broken connection: the name stays, the colour mutes
        self.assertEqual(switcher.dialog_title("connected", frozen=True), (switcher.TITLE_SWITCH, "mute"))

    def test_reason_names_the_fix(self):
        self.assertIn("key", switcher.dialog_reason({}, "auth_required"))
        self.assertIn("8080", switcher.dialog_reason({"port": 8080}, "disconnected"))

    def test_port_words_match_the_launch_screens(self):
        self.assertEqual(switcher.port_is_current(8081, 8081), True)
        self.assertEqual(switcher.port_is_current(8081, None), False)
        self.assertEqual(switcher.port_notice("abc")[0], "error")
        self.assertEqual(switcher.port_notice("70000")[0], "error")
        self.assertEqual(switcher.port_notice("80")[0], "warn")
        self.assertIsNone(switcher.port_notice("8081"))


class TestDialogPortFlow(unittest.TestCase):
    MODELS = [{"id": "m1", "label": "Model 1", "port": 8000}]

    async def _open(self, app, pilot, screen, results) -> None:
        """Push the dialog and wait until its widgets exist; keys sent before that miss the screen."""
        app.push_screen(screen, results.append)
        for _ in range(50):
            await pilot.pause(0.1)
            if app.screen is screen and screen.is_mounted and screen.query("#portfield"):
                return
        raise AssertionError("the dialog did not mount")

    async def _dialog(self, can_write: bool, keys, expect: str | None) -> None:
        screen = switcher.ModelSwitchScreen({"port": 8000}, self.MODELS, "connected", can_write=can_write)
        results: list = []
        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            await self._open(app, pilot, screen, results)
            for key in keys:
                await pilot.press(key)
                await pilot.pause(0.2)
            self.assertEqual(results, [expect])

    def test_menu_row_opens_the_port_field(self):
        # choosing the port row asks for a port instead of closing the dialog
        asyncio.run(self._dialog(False, ["down", "enter", "1", "9", "0", "0", "0", "enter"], "port:19000"))

    def test_monitor_that_cannot_write_only_repoints(self):
        # started with --url: nothing on disk to write, so Enter points the monitor and closes
        asyncio.run(self._dialog(False, ["p", "1", "8", "0", "9", "9", "enter"], "port:18099"))

    def test_writable_monitor_confirms_before_it_writes(self):
        asyncio.run(self._dialog(True, ["p", "1", "8", "0", "9", "9", "enter", "y"], "port:18099:yes"))

    def test_confirm_no_steps_back_to_the_field(self):
        asyncio.run(self._dialog(True, ["p", "1", "8", "0", "9", "9", "enter", "n", "escape", "escape"], None))

    def test_a_bad_port_stays_open(self):
        asyncio.run(self._dialog(True, ["p", "a", "b", "c", "enter", "escape", "escape"], None))

    def test_typing_the_current_port_changes_nothing(self):
        asyncio.run(self._dialog(True, ["p", "8", "0", "0", "0", "enter", "escape"], None))

    def test_port_field_starts_with_the_current_port(self):
        async def exercise():
            from textual.widgets import Input
            screen = switcher.ModelSwitchScreen({"port": 8000}, self.MODELS, "connected", can_write=True)
            results: list = []
            app = App()
            async with app.run_test(size=(100, 30)) as pilot:
                await self._open(app, pilot, screen, results)
                await pilot.press("p")
                await pilot.pause(0.2)
                field = screen.query_one("#portfield", Input)
                value, shown = field.value, field.display
                await pilot.press("enter")           # Enter keeps the current port: no launch, no close
                await pilot.pause(0.2)
                return value, shown, results, app.screen is screen, screen.notice
        value, shown, results, still_open, notice = asyncio.run(exercise())
        self.assertEqual(value, "8000")
        self.assertTrue(shown)
        self.assertEqual(results, [])
        self.assertTrue(still_open)
        self.assertIn("Still on port 8000", notice)

    def test_an_empty_port_is_explained(self):
        async def exercise():
            screen = switcher.ModelSwitchScreen({"port": 8000}, self.MODELS, "connected", can_write=True)
            results: list = []
            app = App()
            async with app.run_test(size=(100, 30)) as pilot:
                await self._open(app, pilot, screen, results)
                await pilot.press("p", "delete", "delete", "delete", "delete", "enter")
                await pilot.pause(0.2)
                return results, app.screen is screen, screen.notice
        results, still_open, notice = asyncio.run(exercise())
        self.assertEqual(results, [])
        self.assertTrue(still_open)
        self.assertIn("Type a port number", notice)
        self.assertNotIn("''", notice)                 # never show an empty pair of quotes as the mistake

    def test_enter_during_the_question_does_not_start_a_model(self):
        asyncio.run(self._dialog(True, ["p", "1", "8", "0", "9", "9", "enter", "enter", "y"],
                                 "port:18099:yes"))


class TestPortDefaults(unittest.TestCase):
    def test_the_default_port_is_8000(self):
        from strata_cli.launch import settings as S
        self.assertEqual(S.DEFAULT_PORT, 8000)
        self.assertEqual(S.LaunchSettings().port, 8000)
        self.assertEqual(S.LaunchSettings.from_dict({"name": "D", "port": ""}).port, 8000)
        self.assertEqual(S.LaunchSettings.from_dict({"name": "D", "port": None}).port, 8000)
        self.assertIn("8000", S.check_port("").next)
        self.assertEqual(S.port_number(""), 8000)
        self.assertEqual(S.port_number(None), 8000)
        self.assertEqual(S.port_number("70000"), 8000)
        self.assertEqual(S.port_number("9123"), 9123)

    def test_usable_port_skips_empty_and_out_of_range_values(self):
        self.assertEqual(switcher.usable_port("", None, "70000", "18099"), 18099)
        self.assertEqual(switcher.usable_port("8000"), 8000)
        self.assertIsNone(switcher.usable_port("", None, 0))

    def test_an_empty_model_port_never_reaches_the_server(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strata-x.json"
            path.write_text(json.dumps({"model_name": "X"}), encoding="utf-8")
            captured = {}

            def fake_popen(cmd, **kwargs):
                captured["cmd"] = cmd
                return object()

            with mock.patch.object(switcher.subprocess, "Popen", fake_popen):
                switcher.launch_model({"id": "x", "path": str(path), "port": "", "data": {}}, "")
            cmd = captured["cmd"]
            self.assertEqual(cmd[cmd.index("--port") + 1], "8000")


class TestMonitorPortChange(unittest.TestCase):
    def setUp(self):
        self.calls: dict = {}
        self.preset = SimpleNamespace(port=8080)
        self.model = {"id": "m1", "label": "Model 1", "port": 8080, "model_name": "Model 1",
                      "data": {"api_key": "secret"}}

        def fake_load(name):
            self.calls["load"] = name
            return self.preset

        def fake_save(preset, name):
            self.calls["save"] = (name, preset.port)
            return preset

        self.store = mock.patch.object(launch_store, "load", fake_load)
        self.store.start()
        self.addCleanup(self.store.stop)
        self.store_save = mock.patch.object(launch_store, "save", fake_save)
        self.store_save.start()
        self.addCleanup(self.store_save.stop)

        def fake_stop(port):
            self.calls["stop"] = port
            return True

        def fake_launch(model, port):
            self.calls["launch"] = (model["id"], port)

        async def fake_ready(port, host="127.0.0.1", timeout=120.0, key=None):
            self.calls["ready"] = (port, key)
            return True

        for name, fn in (("stop_server_on_port", fake_stop), ("launch_model", fake_launch),
                         ("wait_for_server_ready", fake_ready), ("discover_models", lambda *a: [self.model])):
            patcher = mock.patch.object(switcher, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _app(self, info: dict | None):
        app = LaunchMonitor(Endpoint("http://127.0.0.1:8080"), None, info=info or {}, interval=30)
        app._paint = lambda *a, **k: None
        app._schedule_poll = lambda *a, **k: None
        app.latest = {"model": {"id": "m1"}}     # what the monitor believes is answering
        return app

    def test_a_writable_monitor_writes_the_preset_and_restarts(self):
        app = self._app({"name": "Demo"})
        asyncio.run(app._perform_port_change(18099, True))
        self.assertEqual(self.calls.get("save"), ("Demo", 18099))
        self.assertEqual(self.calls.get("stop"), 8080)            # the old port's server is stopped
        self.assertEqual(self.calls.get("launch"), ("m1", 18099))  # the same model, on the new port
        self.assertEqual(self.calls.get("ready"), (18099, "secret"))
        self.assertEqual(app.endpoint.root, "http://127.0.0.1:18099")
        self.assertEqual(app.endpoint.credential, "secret")       # the restart takes the config's key

    def test_a_monitor_started_with_a_url_only_repoints(self):
        app = self._app(None)
        app.endpoint = Endpoint("http://127.0.0.1:8080", "kept-key")
        asyncio.run(app._perform_port_change(18099, True))         # confirmed, but nothing to write
        self.assertNotIn("save", self.calls)
        self.assertNotIn("stop", self.calls)
        self.assertEqual(app.endpoint.root, "http://127.0.0.1:18099")
        self.assertEqual(app.endpoint.credential, "kept-key")     # the old credential still fits the old server
        self.assertIn("not moved", app.selection_notice)

    def test_typing_the_current_port_does_nothing(self):
        app = self._app({"name": "Demo"})
        asyncio.run(app._perform_port_change(8080, True))
        self.assertNotIn("stop", self.calls)
        self.assertNotIn("save", self.calls)
        self.assertEqual(app.endpoint.root, "http://127.0.0.1:8080")


if __name__ == "__main__":
    unittest.main()
