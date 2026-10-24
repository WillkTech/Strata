"""Model switcher engine and modal dialog for the Strata terminal monitor.

This is the Python replacement for an early `switch.sh`: it discovers configurations, reports
the active server, stops it, and launches new model instances. The monitor calls these, and
`ModelSwitchScreen` is the dialog it opens with the `m` key.

Paths are derived from this file's location, so moving the repository (or the checkout that
`strata-*.json` configs live in) does not leave the switcher looking at the old one.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from strata_cli.launch.settings import check_port
from strata_cli.launch.widgets import DARK, LIGHT, Item, Menu

#: The checkout this module lives in (strata_cli/..), used for the default model search and the log folder.
REPO_ROOT = Path(__file__).resolve().parents[1]


def get_server_pid(port: int) -> int | None:
    """Find the PID of the process listening on `port` using lsof -sTCP:LISTEN.
    
    Equivalent to get_server_pid() in switch.sh:
    lsof -ti ":$PORT" -sTCP:LISTEN 2>/dev/null | head -n 1
    """
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        lines = out.strip().split()
        return int(lines[0]) if lines else None
    except Exception:
        return None


def stop_server_on_port(port: int, timeout: float = 15.0) -> bool:
    """Gracefully terminate any Strata server listening on `port`, force-killing if needed.
    
    Equivalent to stop_server() in switch.sh.
    """
    pid = get_server_pid(port)
    if pid is None:
        return True

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        pass

    start = time.time()
    while time.time() - start < timeout:
        if get_server_pid(port) is None:
            return True
        time.sleep(0.5)

    # Force kill if still holding port
    pid_left = get_server_pid(port)
    if pid_left is not None:
        try:
            os.kill(pid_left, signal.SIGKILL)
            time.sleep(0.5)
        except OSError:
            pass

    return get_server_pid(port) is None


def query_metrics(port: int, host: str = "127.0.0.1", key: str | None = None, timeout: float = 2.0) -> dict[str, Any] | None:
    """Query /metrics or /health from the server on `port`."""
    url = f"http://{host}:{port}/metrics"
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        # Fallback to /health
        try:
            h_req = urllib.request.Request(f"http://{host}:{port}/health", headers=headers)
            with urllib.request.urlopen(h_req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return None


def get_active_server_status(port: int, host: str = "127.0.0.1", key: str | None = None) -> dict[str, Any]:
    """Return status summary dict matching switch.sh status_view."""
    pid = get_server_pid(port)
    if pid is None:
        return {
            "badge": "STOPPED",
            "detail": f"no Strata server on port {port}",
            "pid": None,
            "port": port,
        }

    m = query_metrics(port, host=host, key=key)
    if not m:
        return {
            "badge": "STARTING",
            "detail": f"PID {pid} on port {port} — /metrics not answering yet",
            "pid": pid,
            "port": port,
        }

    live = m.get("live") or {}
    eng = m.get("engine") or {}
    hw = m.get("hardware") or {}
    state = (live.get("state") or m.get("status") or "running").upper()

    bits = [state]
    if live.get("tok_s"):
        bits.append(f"{live['tok_s']} tk/s")
    if live.get("generated") is not None:
        bits.append(f"{live['generated']} tok")

    model = eng.get("model") or m.get("model") or "?"
    detail_parts = [str(model)]

    ctx = int(eng.get("max_context") or m.get("max_context") or 0)
    if ctx:
        detail_parts.append(f"ctx {ctx // 1024}k" if ctx >= 1024 else f"ctx {ctx}")
    if hw.get("gpu_util") is not None:
        detail_parts.append(f"GPU {hw['gpu_util']}%")
    detail_parts.append(f"PID {pid}")

    return {
        "badge": " ".join(bits),
        "detail": " | ".join(detail_parts),
        "pid": pid,
        "port": port,
        "model": model,
        "max_context": ctx,
    }


def _describe_config(path: str, data: dict) -> tuple[str, str]:
    """Generate (id, label) for a strata-*.json config file."""
    base = os.path.basename(path)
    model_name = data.get("model_name", "")
    args = data.get("args", [])

    ctx = 0
    if "--max-context" in args:
        try:
            ctx = int(args[args.index("--max-context") + 1])
        except (ValueError, IndexError):
            pass

    has_yarn = "--rope-scaling" in args and "yarn" in args
    has_vision = "--vision" in args or "vision" in data

    # Human-friendly label formatting
    if "m5-64k" in base or ctx == 65536:
        label = "Swift 1.5 Abliterated IQ3_S (64k context, vision) [Daily]"
    elif "swift" in base.lower():
        ctx_str = f"{ctx // 1024}k" if ctx else "512k"
        extra = "yarn" if has_yarn else "standard"
        label = f"Swift 1.5 Abliterated IQ3_S ({ctx_str} context, vision, {extra})"
    elif "qwen" in base.lower():
        ctx_str = f"{ctx // 1024}k" if ctx else "512k"
        extra = "yarn" if has_yarn else "standard"
        label = f"Qwen 3.8 Flash Next IQ3_S ({ctx_str} context, vision, {extra})"
    else:
        ctx_str = f", {ctx // 1024}k ctx" if ctx else ""
        vis_str = ", vision" if has_vision else ""
        label = f"{model_name or base}{ctx_str}{vis_str}"

    return base, label


def discover_models(search_dirs: list[str] | None = None) -> list[dict[str, Any]]:
    """Discover available model configurations and runner scripts.
    
    Includes root-level strata-*.json configs.
    """
    if search_dirs is None:
        search_dirs = [
            os.getcwd(),
            str(REPO_ROOT),
        ]

    models: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        pattern = os.path.join(d, "strata-*.json")
        for path in sorted(glob.glob(pattern)):
            canon = os.path.realpath(path)
            # Skip old 0.2.1 configs if present to ensure only current v0.2.2 models appear
            base = os.path.basename(canon)
            if canon in seen_paths or base in ("strata-iq3_s-512k.json", "strata-iq3_s.json"):
                continue
            seen_paths.add(canon)
            try:
                with open(canon, "r", encoding="utf-8") as f:
                    data = json.load(f)
                config_id, label = _describe_config(canon, data)
                models.append({
                    "id": config_id,
                    "label": label,
                    "type": "config",
                    "path": canon,
                    "data": data,
                    "model_name": data.get("model_name", config_id),
                    "port": int(data.get("port") or 8080),
                })
            except Exception:
                continue

    return models


def config_api_key(item: dict[str, Any]) -> str:
    """The key a config's server accepts, for the monitor that launched it.

    serve/server.py takes the key from --api-key, STRATA_API_KEY or the config's "api_key":
    a string (several separated by commas), a list, or "none" for no key.
    """
    raw = (item.get("data") or {}).get("api_key", "")
    if isinstance(raw, (list, tuple)):
        raw = next((str(v) for v in raw if str(v).strip()), "")
    raw = str(raw or "").strip()
    if not raw or raw.lower() == "none":
        return ""
    return raw.split(",")[0].strip()


def launch_model(item: dict[str, Any], port: int | None = None, python_exe: str | None = None) -> subprocess.Popen:
    """Launch a server model in the background, matching switch.sh start_server_background."""
    py = python_exe or sys.executable
    target_port = port or item.get("port") or 8080

    if item.get("type") == "script":
        script = item["path"]
        arg = item.get("arg", "")
        cmd = ["/bin/sh", script, arg] if arg else ["/bin/sh", script]
        log_dir = str(REPO_ROOT / "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"{item['id']}.log")
        log_file = open(log_path, "a", encoding="utf-8")
        return subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    # Config-based launch
    config_path = item["path"]
    data = item.get("data") or {}
    log_path = data.get("log")
    if not log_path:
        log_dir = os.path.join(os.path.dirname(config_path), "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"{os.path.splitext(os.path.basename(config_path))[0]}.log")
    else:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

    log_file = open(log_path, "a", encoding="utf-8")
    cmd = [
        py,
        "-m",
        "serve.server",
        "--engine",
        "strata",
        "--config",
        config_path,
        "--port",
        str(target_port),
    ]

    env = dict(os.environ)
    env.pop("STRATA_API_KEY", None)

    return subprocess.Popen(
        cmd,
        cwd=data.get("cwd") or os.path.dirname(config_path) or ".",
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )


async def wait_for_server_ready(port: int, host: str = "127.0.0.1", timeout: float = 120.0, key: str | None = None) -> bool:
    """Poll the server until /metrics or /health answers OK."""
    start = time.time()
    while time.time() - start < timeout:
        m = await asyncio.to_thread(query_metrics, port, host=host, key=key, timeout=1.5)
        if m is not None:
            return True
        await asyncio.sleep(1.5)
    return False


class ModelSwitchScreen(ModalScreen):
    """Interactive model switcher dialog inside the terminal monitor."""

    BINDINGS = [
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("enter", "pick", show=False),
        Binding("escape", "cancel", show=False),
        Binding("q", "cancel", show=False),
    ]
    DEFAULT_CSS = """
    ModelSwitchScreen { align: center middle; background: #0e1113 85%; }
    #dialog { width: 80; max-width: 100%; height: auto; max-height: 100%; background: #161a1d; border: solid #34d399; padding: 1 2; }
    """

    def __init__(self, active_status: dict[str, Any], models: list[dict[str, Any]]):
        super().__init__()
        self.active_status = active_status
        self.models = models
        self.options: list[tuple[str, str]] = []
        for i, m in enumerate(models, 1):
            key = f"model:{m['id']}"
            prefix = f"{i}) " if i < 10 else "   "
            self.options.append((key, f"{prefix}{m['label']}"))
        self.options.append(("stop", "s) Stop active server (free GPU memory)"))
        self.options.append(("cancel", "q) Cancel / Stay in monitor"))

    def compose(self) -> ComposeResult:
        pal = LIGHT if self.app.has_class("theme-light") else DARK
        badge = self.active_status.get("badge", "STOPPED")
        detail = self.active_status.get("detail", "no active server")
        with Vertical(id="dialog"):
            yield Static(Text("Strata Model Switcher", style=f"bold {pal.accent}"), markup=False)
            yield Static(Text(f"Currently active: [{badge}] {detail}", style=pal.mute), markup=False)
            yield Static("", markup=False)
            yield Menu(id="confirm-menu", palette=pal)
            yield Static(Text("1-9 / Up/Down choose   Enter switch   s stop   Esc/q cancel", style=pal.mute), markup=False)

    def on_mount(self) -> None:
        m = self.query_one(Menu)
        m.set_items([Item([label], key=key) for key, label in self.options], 0)
        if self.app.has_class("theme-light"):
            dialog = self.query_one("#dialog")
            dialog.styles.background = LIGHT.panel
            dialog.styles.border = ("solid", LIGHT.accent)
            dialog.styles.color = LIGHT.fg
            self.styles.background = "#f9fafb 70%"
            for static in dialog.query(Static):
                static.styles.background = LIGHT.panel

    def on_key(self, event) -> None:
        key = event.key
        if key in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
            idx = int(key) - 1
            if idx < len(self.models):
                self.dismiss(f"model:{self.models[idx]['id']}")
                event.stop()
        elif key == "s":
            self.dismiss("stop")
            event.stop()
        elif key in ("q", "escape"):
            self.dismiss(None)
            event.stop()

    def action_move(self, step: int) -> None:
        self.query_one(Menu).move(step)

    def action_pick(self) -> None:
        curr = self.query_one(Menu).current
        self.dismiss(curr.key if curr else None)

    def action_cancel(self) -> None:
        self.dismiss(None)
