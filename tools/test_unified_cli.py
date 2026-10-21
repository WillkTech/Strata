"""Unit tests for the unified Strata CLI, Model Management, and Monitor resolution.

Validates:
- Deterministic connection resolution & credential precedence
- Local instance discovery & ambiguity handling
- Model manager profile inspection & key masking security
- Process safety guard (refusing to terminate unrelated processes)
- CLI zero-argument non-TTY vs TTY behavior
- Unified subcommands ('switch', 'key', 'monitor', 'status')
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strata_cli import connections, model_manager
from strata_cli.cli import main, parser


class TestConnectionResolution(unittest.TestCase):
    def test_explicit_url_flag_wins(self):
        res = connections.resolve(url="http://127.0.0.1:9000", environment={"STRATA_URL": "http://127.0.0.1:8000"})
        self.assertEqual(res["url"], "http://127.0.0.1:9000")
        self.assertEqual(res["origins"]["url"], "flag")

    def test_explicit_credential_env_flag_wins(self):
        res = connections.resolve(credential_env="CUSTOM_KEY", environment={"STRATA_KEY": "abc", "STRATA_API_KEY": "def"})
        self.assertEqual(res["credential_env"], "CUSTOM_KEY")
        self.assertEqual(res["origins"]["credential_env"], "flag")

    def test_env_precedence_strata_key(self):
        env = {"STRATA_KEY": "my_secret_key"}
        res = connections.resolve(environment=env)
        self.assertEqual(res["credential_env"], "STRATA_KEY")
        self.assertEqual(res["origins"]["credential_env"], "environment")
        self.assertTrue(res["credential_present"])

    def test_env_precedence_strata_api_key(self):
        env = {"STRATA_API_KEY": "my_api_key"}
        res = connections.resolve(environment=env)
        self.assertEqual(res["credential_env"], "STRATA_API_KEY")
        self.assertEqual(res["origins"]["credential_env"], "environment")
        self.assertTrue(res["credential_present"])

    def test_discovery_single_active_port(self):
        def fake_probe(url, timeout=0.5):
            if ":8000" in url:
                return {"is_strata": True, "state": "ready", "status": 200}
            return {"is_strata": False, "state": "unreachable", "status": None}

        with patch("strata_cli.connections.probe_server", side_effect=fake_probe):
            res = connections.resolve(environment={})
            self.assertEqual(res["url"], "http://127.0.0.1:8000")
            self.assertEqual(res["origins"]["url"], "local_discovery")

    def test_discovery_ambiguity_detected(self):
        def fake_probe(url, timeout=0.5):
            return {"is_strata": True, "state": "ready", "status": 200}

        with patch("strata_cli.connections.probe_server", side_effect=fake_probe):
            res = connections.resolve(environment={})
            self.assertTrue(res.get("ambiguity"))
            self.assertEqual(res["origins"]["url"], "local_discovery_ambiguous")
            self.assertEqual(res["discovered_ports"], [8000, 8095])


class TestModelManager(unittest.TestCase):
    def test_profiles_inspection(self):
        profiles = model_manager.get_all_profiles()
        p_ids = [p["id"] for p in profiles]
        self.assertIn("swift", p_ids)
        self.assertIn("qwen", p_ids)

        swift = next(p for p in profiles if p["id"] == "swift")
        self.assertEqual(swift["port"], 8000)
        self.assertEqual(swift["context_tokens"], 524288)
        self.assertTrue(swift["vision"])

    def test_mask_key_security(self):
        self.assertEqual(model_manager.mask_key(None), "none")
        self.assertEqual(model_manager.mask_key(""), "none")
        self.assertEqual(model_manager.mask_key("12345"), "*** (5 chars)")
        masked = model_manager.mask_key("9d9d84aaecb6ff22d313b5e784bcd411e1cf423def12bbfdfb82ad1d7f334df5")
        self.assertTrue(masked.startswith("9d9d...4df5"))
        self.assertNotIn("b6ff22", masked)
        self.assertIn("64 chars", masked)

    def test_refuse_kill_unrelated_process(self):
        with patch("strata_cli.model_manager.get_server_pid", return_value=(99999, False)):
            with patch("strata_cli.model_manager.get_process_command", return_value="redis-server *:8000"):
                res = model_manager.stop_server(8000)
                self.assertFalse(res["ok"])
                self.assertEqual(res["error"], "port_conflict_unrelated_process")
                self.assertIn("Refusing to kill", res["message"])


class TestCLIIntegration(unittest.TestCase):
    def test_cli_parser_has_subcommands(self):
        ap = parser()
        subparsers = [a for a in ap._actions if a.dest == "command"][0]
        cmds = list(subparsers.choices.keys())
        self.assertIn("switch", cmds)
        self.assertIn("key", cmds)
        self.assertIn("monitor", cmds)
        self.assertIn("status", cmds)
        self.assertIn("doctor", cmds)
        self.assertIn("models", cmds)

    def test_non_tty_zero_args_fails_with_usage(self):
        with patch("sys.stdin.isatty", return_value=False):
            with patch("sys.stdout.isatty", return_value=False):
                with self.assertRaises(SystemExit) as ctx:
                    main([])
                self.assertEqual(ctx.exception.code, 2)

    def test_switch_status_command(self):
        code = main(["switch", "status", "--json"])
        self.assertEqual(code, 0)

    def test_key_status_command(self):
        code = main(["key", "status", "--json"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
