"""Model switcher engine for Strata terminal and monitor.

References `/Volumes/vl/ai/Strata/switch.sh` to enable switching between models,
discovering configurations, stopping active servers, and launching new model instances
directly from Python and the TUI monitor.
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
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from strata_cli.launch.widgets import DARK, LIGHT, Item, Menu


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
            "/Volumes/vl/ai/Strata",
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


def launch_model(item: dict[str, Any], port: int | None = None, python_exe: str | None = None) -> subprocess.Popen:
    """Launch a server model in the background, matching switch.sh start_server_background."""
    py = python_exe or sys.executable
    target_port = port or item.get("port") or 8080

    if item.get("type") == "script":
        script = item["path"]
        arg = item.get("arg", "")
        cmd = ["/bin/sh", script, arg] if arg else ["/bin/sh", script]
        log_dir = "/Volumes/vl/ai/Strata/logs"
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


# ------------------------------------------------------------------------------------------------ CLI entrypoint

def run_cli_menu() -> None:
    """Standalone terminal menu for switching models, stopping, or attaching to monitor.
    
    Equivalent to switch.sh in interactive mode, but natively supporting v0.2.2 models.
    """
    models = discover_models()
    port = 8080
    if models and models[0].get("port"):
        port = models[0]["port"]

    target = sys.argv[1].lower().strip() if len(sys.argv) > 1 else ""

    # Direct CLI arg execution, e.g.: llm stop, llm 1, llm swift, llm 64k
    if target:
        if target in ("stop", "kill"):
            print(f"Stopping active Strata server on port {port}...")
            stop_server_on_port(port)
            print(f"Port {port} is free.")
            return

        if target in ("status", "ps"):
            st = get_active_server_status(port)
            print(f"[{st.get('badge')}] {st.get('detail')}")
            return

        if target in ("monitor", "top"):
            from strata_cli.monitor_commands import main as monitor_main
            sys.exit(monitor_main(["monitor", "--url", f"http://127.0.0.1:{port}"]))

        # Check by index (1, 2, 3...)
        if target.isdigit():
            idx = int(target) - 1
            if 0 <= idx < len(models):
                _switch_and_monitor(models[idx], port)
                return

        # Check by substring (swift, qwen, 64k, 512k)
        match = next((m for m in models if target in m["id"].lower() or target in m["label"].lower()), None)
        if match:
            _switch_and_monitor(match, port)
            return

        print(f"Unknown command or model: {target}")
        print("Usage: llm [1-3 | swift | qwen | 64k | status | stop | monitor]")
        sys.exit(1)

    # Interactive CLI menu loop (like switch.sh)
    while True:
        os.system("clear")
        st = get_active_server_status(port)
        badge = st.get("badge", "STOPPED")
        detail = st.get("detail", f"no Strata server on port {port}")

        print("==================================================")
        print(" Strata Model Switcher")
        print("==================================================")
        print("Currently active:")
        print(f"\033[7m {badge} \033[27m")
        print(detail)
        print("")
        print("Select a model to start:")
        for i, m in enumerate(models, 1):
            print(f"  {i}) {m['label']}")
        print("  s) Stop active server")
        print("  m) Live TUI monitor")
        print("  q) Quit")
        print("")

        try:
            choice = input(f"Choice [1-{len(models)}, s, m, q]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("")
            break

        if not choice or choice in ("q", "quit", "exit"):
            break

        if choice in ("s", "stop"):
            print(f"\nStopping server on port {port}...")
            stop_server_on_port(port)
            print(f"Port {port} is free.")
            time.sleep(1)
            continue

        if choice in ("m", "monitor"):
            from strata_cli.monitor_commands import main as monitor_main
            try:
                monitor_main(["monitor", "--url", f"http://127.0.0.1:{port}"])
            except Exception:
                pass
            continue

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                _switch_and_monitor(models[idx], port)
                break

        # Check keyword
        match = next((m for m in models if choice in m["id"].lower() or choice in m["label"].lower()), None)
        if match:
            _switch_and_monitor(match, port)
            break


def _switch_and_monitor(model: dict[str, Any], port: int) -> None:
    label = model.get("label", model["id"])
    target_port = model.get("port") or port

    print(f"\nStopping any active server on port {target_port}...")
    stop_server_on_port(target_port)

    print(f"Starting {label} on port {target_port}...")
    launch_model(model, target_port)

    print("Waiting for server to be ready", end="", flush=True)
    start = time.time()
    ready = False
    while time.time() - start < 120.0:
        if query_metrics(target_port, timeout=1.5) is not None:
            ready = True
            break
        print(".", end="", flush=True)
        time.sleep(1.5)

    if ready:
        print(" — ready!")
        time.sleep(0.5)
        print("Opening monitor (press 'm' anytime to switch models, 'q' to detach)...")
        time.sleep(1)
        from strata_cli.monitor_commands import main as monitor_main
        sys.exit(monitor_main(["monitor", "--url", f"http://127.0.0.1:{target_port}"]))
    else:
        print(f"\nServer failed to become ready within 120s. Check logs.")
        sys.exit(1)


if __name__ == "__main__":
    run_cli_menu()
