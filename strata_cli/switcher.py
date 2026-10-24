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

from strata_cli.tui import DARK, LIGHT, Item, Menu

#: The checkout this module lives in (strata_cli/..), used for the default model search and the log folder.
REPO_ROOT = Path(__file__).resolve().parents[1]


#: The port a model is served on unless a config, preset or the person at the keyboard says otherwise.
DEFAULT_PORT = 8000


def usable_port(*values: Any) -> int | None:
    """The first value that is a real TCP port; None when none is.

    A config or a saved preset may carry "", None or a string. Passing that to `--port`
    would start the server with an empty argument, so every caller goes through here.
    """
    for value in values:
        try:
            port = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            return port
    return None


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


def get_server_config_path(pid: int) -> str | None:
    """Find the --config path of a running server process from its command line."""
    args = None
    try:
        from strata_control import procs
        args = procs.command_line(pid)
    except Exception:
        pass
    if not args:
        try:
            out = subprocess.check_output(
                ["ps", "-o", "command=", "-p", str(pid)],
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            args = out.split()
        except Exception:
            args = None
    if args and "--config" in args:
        idx = args.index("--config")
        if idx + 1 < len(args):
            return args[idx + 1]
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
        try:
            from strata_control import paths
            launch_d = str(paths.state_base() / "launch")
            if os.path.isdir(launch_d) and launch_d not in search_dirs:
                search_dirs.append(launch_d)
        except Exception:
            pass

    models: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        patterns = [os.path.join(d, "strata-*.json")]
        if os.path.basename(d) == "launch":
            patterns.append(os.path.join(d, "*.json"))
        for pattern in patterns:
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
                    if not isinstance(data, dict) or ("exe" not in data and "engine" not in data and "model_name" not in data):
                        continue
                    config_id, label = _describe_config(canon, data)
                    models.append({
                        "id": config_id,
                        "label": label,
                        "type": "config",
                        "path": canon,
                        "data": data,
                        "model_name": data.get("model_name", config_id),
                        "port": usable_port(data.get("port")) or DEFAULT_PORT,
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
    target_port = usable_port(port, item.get("port")) or DEFAULT_PORT

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


#: The dialog's two names. The name follows the action: "Switcher" when there is something to switch
#: away from, "Launcher" when the only useful thing to do is start one. Colour carries the urgency.
TITLE_SWITCH = "Strata Model Switcher"
TITLE_LAUNCH = "Strata Model Launcher"

#: the monitor's connection state -> the palette key to draw the title in
_STATE_STYLE = {
    "connected": "accent",
    "connecting": "warn",
    "disconnected": "bad",
    "auth_required": "warn",
    "incompatible": "warn",
    "stale": "warn",
    "telemetry_unavailable": "warn",
}

#: what each state means, in plain words, because the fixes differ
_STATE_REASON = {
    "auth_required": "a server is answering; the key is missing or wrong",
    "incompatible": "a server is answering; its telemetry schema is older",
    "stale": "a server is answering; its samples are old",
    "telemetry_unavailable": "a server is answering; telemetry is off",
}


def dialog_title(connection_state: str, *, frozen: bool = False) -> tuple[str, str]:
    """(title, palette key). Frozen keeps the name and mutes the colour: nothing is wrong, the display is."""
    name = TITLE_LAUNCH if connection_state in ("connecting", "disconnected") else TITLE_SWITCH
    style = "mute" if frozen else _STATE_STYLE.get(connection_state, "accent")
    return name, style


def dialog_reason(status: dict[str, Any], connection_state: str) -> str:
    """Why the dialog says what it says, so a user can tell an idle monitor from a broken one."""
    if connection_state in _STATE_REASON:
        return _STATE_REASON[connection_state]
    if connection_state in ("connecting", "disconnected"):
        return f"nothing is answering on port {status.get('port')}"
    return str(status.get("detail") or "no active server")


def port_is_current(port: int, current: int | None) -> bool:
    """True when the typed port is the one already in use."""
    return current is not None and int(port) == int(current)


def port_notice(raw: str) -> tuple[str, str] | None:
    """("error" | "warn", sentence) for a typed port, or None when it is usable.

    The launch screens use launch.settings.check_port for the same words; this copy keeps the
    modal's import graph inside the thin bundle, which carries no `strata_cli.launch`.
    """
    try:
        text = str(raw).strip()
        if not text:
            return "error", "Type a port number from 1 to 65535, for example 8000."
        p = int(text)
    except (TypeError, ValueError):
        return "error", f"'{raw}' is not a port number. Use a number from 1 to 65535, for example 8000."
    if not 1 <= p <= 65535:
        return "error", f"Port {p} does not exist. Use a number from 1 to 65535, for example 8000."
    if p < 1024:
        return "warn", (f"Port {p} is below 1024; on many computers only an administrator can use it. "
                        "8000 works without special rights.")
    return None


class ModelSwitchScreen(ModalScreen):
    """Interactive model switcher dialog inside the terminal monitor."""

    BINDINGS = [
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("enter", "pick", show=False),
        Binding("escape", "cancel", show=False),
        Binding("q", "cancel", show=False),
        Binding("p", "port", show=False),
        Binding("y", "confirm_yes", show=False),
        Binding("n", "confirm_no", show=False),
    ]
    DEFAULT_CSS = """
    ModelSwitchScreen { align: center middle; background: #0e1113 85%; }
    #dialog { width: 80; max-width: 100%; height: auto; max-height: 100%; background: #161a1d; border: solid #34d399; padding: 1 2; }
    #portfield { height: 3; border: solid #34d399; display: none; }
    """

    def __init__(self, active_status: dict[str, Any], models: list[dict[str, Any]],
                 connection_state: str = "connected", *, frozen: bool = False, can_write: bool = False,
                 preset_name: str | None = None, live: int = 0) -> None:
        super().__init__()
        self.active_status = active_status
        self.models = models
        self.connection_state = connection_state
        self.frozen = frozen
        self.can_write = can_write            # only this monitor may write the preset to disk
        self.preset_name = preset_name
        self.live = live
        self.confirm_port: int | None = None
        self.port_open = False               # the port field is showing: Esc steps back to the list
        self.notice = ""
        self.options: list[tuple[str, str, bool]] = []
        for i, m in enumerate(models, 1):
            key = f"model:{m['id']}"
            prefix = f"{i}) " if i < 10 else "   "
            self.options.append((key, f"{prefix}{m['label']}", True))
        # Nothing to stop when nothing is running, so the row is there but grey.
        self.options.append(("stop", "s) Stop active server (free GPU memory)", self.has_active_server()))
        self.options.append(("port", f"p) Change port (now {self.current_port()}) ...", True))
        self.options.append(("cancel", "q) Cancel / Stay in monitor", True))

    def has_active_server(self) -> bool:
        """A server is answering on the port the monitor watches."""
        return self.active_status.get("pid") is not None and self.connection_state not in ("connecting", "disconnected")

    def palette(self) -> Any:
        return LIGHT if self.app.has_class("theme-light") else DARK

    def compose(self) -> ComposeResult:
        pal = self.palette()
        title, style = dialog_title(self.connection_state, frozen=self.frozen)
        reason = dialog_reason(self.active_status, self.connection_state)
        with Vertical(id="dialog"):
            yield Static(Text(title, style=f"bold {getattr(pal, style)}"), markup=False)
            yield Static(Text(f"Currently active: {reason}", style=pal.mute), markup=False)
            yield Static("", markup=False)
            yield Menu(id="confirm-menu", palette=pal)
            yield Input(placeholder="New port, e.g. 8081   Enter   Esc back", id="portfield")
            yield Static(Text(self.hints(), style=pal.mute), markup=False, id="hint")

    def hints(self) -> str:
        if self.port_open:
            return "Type a port from 1 to 65535.   Enter use this port   Esc back"
        if self.confirm_port is not None:
            cost = (f"the model reloads ({self.live} request{'s' if self.live != 1 else ''} running)"
                    if self.live else "the model reloads")
            target = f"preset '{self.preset_name}'" if self.preset_name and self.can_write else "nothing on disk"
            return f"Move to port {self.confirm_port}? It writes {target} and {cost}.   y yes   n no"
        if self.notice:
            return self.notice
        return "1-9 / Up/Down choose   Enter choose   p port   s stop   Esc/q cancel"

    def on_mount(self) -> None:
        m = self.query_one(Menu)
        m.set_items([Item([label], key=key, enabled=enabled) for key, label, enabled in self.options], 0)
        field = self.query_one("#portfield", Input)
        field.display = False
        field.can_focus = False            # Textual focuses the first input on mount; the hidden one must not take the keys
        self.set_focus(None)
        if self.app.has_class("theme-light"):
            dialog = self.query_one("#dialog")
            dialog.styles.background = LIGHT.panel
            dialog.styles.border = ("solid", LIGHT.accent)
            dialog.styles.color = LIGHT.fg
            self.styles.background = "#f9fafb 70%"
            for static in dialog.query(Static):
                static.styles.background = LIGHT.panel
            self.query_one("#portfield", Input).styles.border = ("solid", LIGHT.accent)

    def on_key(self, event) -> None:
        key = event.key
        field = self.query_one("#portfield", Input)
        if field.display and field.has_focus:      # typing a port: the digits belong to the field
            return
        if self.confirm_port is not None:
            if key == "y":
                self.dismiss(f"port:{self.confirm_port}:yes")
                event.stop()
            elif key in ("n", "q", "escape"):
                self.confirm_port = None
                self.notice = ""
                self.refresh_layout()
                event.stop()
            return
        if key in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
            idx = int(key) - 1
            if idx < len(self.models):
                self.dismiss(f"model:{self.models[idx]['id']}")
                event.stop()
        elif key == "s":
            if self.has_active_server():
                self.dismiss("stop")
                event.stop()
        elif key == "p":
            self.action_port()
            event.stop()
        elif key in ("q", "escape"):
            self.dismiss(None)
            event.stop()

    def action_move(self, step: int) -> None:
        self.query_one(Menu).move(step)

    def action_pick(self) -> None:
        if self.confirm_port is not None:             # the confirm line answers to y and n only
            return
        field = self.query_one("#portfield", Input)
        if field.display:                             # typing a port: Enter submits the field, not the menu
            field.action_submit()
            return
        curr = self.query_one(Menu).current
        if curr and curr.enabled:
            if curr.key == "port":                    # the port row asks for a port; it does not close the dialog
                self.action_port()
                return
            self.dismiss(curr.key)

    def action_cancel(self) -> None:
        if self.port_open:                            # Esc leaves the field before it leaves the dialog
            self._close_field(self.query_one("#portfield", Input))
            self.refresh_layout()
            return
        self.dismiss(None)

    def current_port(self) -> int:
        """The port this dialog would keep if the person pressed Enter: the server's, else the default."""
        return usable_port(self.active_status.get("port")) or DEFAULT_PORT

    def action_port(self) -> None:
        field = self.query_one("#portfield", Input)
        field.value = str(self.current_port())      # Enter keeps it; typing replaces it (focus selects all)
        field.display = True
        field.can_focus = True
        field.focus()
        field.select_all()
        self.port_open = True
        self.refresh_layout()

    def _close_field(self, field: Input) -> None:
        """Hide the port field and leave the dialog's keys (y/n, digits, m) to the screen again."""
        field.display = False
        field.can_focus = False
        field.blur()
        self.port_open = False

    def on_input_changed(self, event: Input.Changed) -> None:
        self.notice = ""

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        notice = port_notice(raw)
        if notice and notice[0] == "error":
            self.notice = notice[1]
            self.refresh_layout()
            return
        port = int(raw)
        if port_is_current(port, self.current_port()):
            self._close_field(event.input)           # nothing to change: step back to the list, keep the dialog open
            self.notice = f"Still on port {port}."
            self.refresh_layout()
            return
        if notice:                              # a warning (a low port) still goes through, as in the launch screens
            self.notice = notice[1]
        if not self.can_write:                  # nothing to write here: only point this monitor somewhere else
            self.dismiss(f"port:{port}")
            return
        self.confirm_port = port
        self._close_field(event.input)                # the confirm line names the port; y/n answer it
        self.refresh_layout()

    def action_confirm_yes(self) -> None:
        if self.confirm_port is not None:
            self.dismiss(f"port:{self.confirm_port}:yes")

    def action_confirm_no(self) -> None:
        if self.confirm_port is not None:
            self.confirm_port = None
            self.notice = ""
            field = self.query_one("#portfield", Input)
            field.display = True
            field.can_focus = True
            field.focus()
            self.port_open = True
            self.refresh_layout()

    def refresh_layout(self) -> None:
        pal = self.palette()
        style = pal.warn if (self.confirm_port is not None or self.notice) else pal.mute
        self.query_one("#hint", Static).update(Text(self.hints(), style=f"bold {style}" if style != pal.mute else style))
