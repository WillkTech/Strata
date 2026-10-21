"""strata_cli.model_manager — Shared model-management service for Strata on Apple Silicon.

Provides a unified control layer for model profiles, launch, graceful stop,
readiness, status inspection, process verification, and API key configuration.
Used by both the CLI (`strata`, `strata switch`) and the monitoring TUI (`strata monitor`).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_PORT = 8000
REPO_ROOT = Path(__file__).resolve().parent.parent


def get_repo_root() -> Path:
    """Return the absolute path to the Strata repository root."""
    candidate = REPO_ROOT
    if (candidate / "strata.py").is_file():
        return candidate
    cwd = Path.cwd().resolve()
    if (cwd / "strata.py").is_file():
        return cwd
    return candidate


def find_python() -> str:
    """Find the preferred Python interpreter for Strata server subprocesses."""
    override = os.environ.get("STRATA_PYTHON")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override

    root = get_repo_root()
    candidates = [
        "/Volumes/vl/ai/.venv/bin/python",
        str(root / ".venv" / "bin" / "python3"),
        str(root / ".venv" / "bin" / "python"),
        str(root / "python" / "bin" / "python3"),
        str(root / "python" / "bin" / "python"),
        sys.executable,
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return sys.executable


# -----------------------------------------------------------------------------
# Model Profiles
# -----------------------------------------------------------------------------

PROFILES = {
    "swift": {
        "id": "swift",
        "aliases": ["1", "swift-1.5", "swift1.5"],
        "display_name": "Swift 1.5 Abliterated IQ3_S",
        "config_file": "strata-iq3_s-512k.json",
        "launch_script": "run-swift.sh",
        "fallback_context": "512k",
    },
    "qwen": {
        "id": "qwen",
        "aliases": ["2", "qwen-3.8", "qwen3.8"],
        "display_name": "Qwen 3.8 Flash Next IQ3_S",
        "config_file": "strata-iq3_s.json",
        "launch_script": "run-qwen.sh",
        "fallback_context": "512k",
    },
}


def canonical_profile_id(name_or_alias: str) -> str | None:
    """Resolve a profile name or numeric alias to canonical ID ('swift' or 'qwen')."""
    target = str(name_or_alias).strip().lower()
    for pid, pdata in PROFILES.items():
        if target == pid or target in pdata["aliases"]:
            return pid
    return None


def inspect_profile(profile_id: str, repo_root: Path | None = None) -> dict[str, Any]:
    """Inspect model configuration dynamically without hardcoded assumptions."""
    root = repo_root or get_repo_root()
    cid = canonical_profile_id(profile_id)
    if not cid:
        raise ValueError(f"Unknown model profile: {profile_id}")

    pdata = PROFILES[cid]
    cfg_path = root / pdata["config_file"]
    script_path = root / pdata["launch_script"]

    result = {
        "id": cid,
        "display_name": pdata["display_name"],
        "config_file": pdata["config_file"],
        "config_path": str(cfg_path),
        "launch_script": pdata["launch_script"],
        "script_path": str(script_path),
        "config_exists": cfg_path.is_file(),
        "script_exists": script_path.is_file(),
        "model_name": pdata["display_name"],
        "port": DEFAULT_PORT,
        "host": "127.0.0.1",
        "context_tokens": 524288,
        "context_display": pdata["fallback_context"],
        "vision": False,
        "api_key_configured": False,
    }

    if cfg_path.is_file():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                result["model_name"] = raw.get("model_name") or result["model_name"]
                result["port"] = int(raw.get("port") or DEFAULT_PORT)
                result["host"] = raw.get("host") or "127.0.0.1"
                result["api_key_configured"] = bool(raw.get("api_key"))

                # Parse context tokens from args
                args = raw.get("args") or []
                if isinstance(args, list):
                    for i, arg in enumerate(args):
                        if arg == "--max-context" and i + 1 < len(args):
                            try:
                                ctx = int(args[i + 1])
                                result["context_tokens"] = ctx
                                result["context_display"] = f"{ctx // 1024}k" if ctx >= 1024 else str(ctx)
                            except ValueError:
                                pass
                        if arg == "--vision":
                            result["vision"] = True

                if raw.get("vision"):
                    result["vision"] = True
        except Exception:
            pass

    return result


def get_all_profiles(repo_root: Path | None = None) -> list[dict[str, Any]]:
    """Return detailed configurations for all available profiles."""
    root = repo_root or get_repo_root()
    return [inspect_profile(pid, root) for pid in ("swift", "qwen")]


# -----------------------------------------------------------------------------
# Process Verification and Lifecycle Management
# -----------------------------------------------------------------------------

def get_process_command(pid: int) -> str:
    """Return the full command line of process pid, or empty string on error."""
    try:
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        return res.stdout.strip()
    except Exception:
        return ""


def is_strata_process(pid: int, cmdline: str | None = None) -> bool:
    """Verify that a process is genuinely a Strata server or engine."""
    cmd = cmdline if cmdline is not None else get_process_command(pid)
    if not cmd:
        return False
    markers = [
        "serve/server.py",
        "serve.server",
        "build-metal/strata",
        "build-metal/strata-vision",
        "run-swift.sh",
        "run-qwen.sh",
        "strata-iq3_s",
    ]
    return any(m in cmd for m in markers)


def get_server_pid(port: int = DEFAULT_PORT) -> tuple[int | None, bool]:
    """Find the listening process on port.

    Returns (pid, is_verified_strata).
    If an unrelated process occupies the port, returns (pid, False).
    """
    try:
        # -sTCP:LISTEN ensures we only get the server daemon, not client sockets
        res = subprocess.run(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        if not lines:
            # Fallback to general lsof if LISTEN filter was too strict
            res = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]

        if not lines:
            return None, False

        pid = int(lines[0])
        cmd = get_process_command(pid)
        verified = is_strata_process(pid, cmd)
        return pid, verified
    except Exception:
        return None, False


def stop_server(port: int = DEFAULT_PORT, timeout: float = 12.0) -> dict[str, Any]:
    """Gracefully stop active Strata server.

    Never kills an arbitrary unrelated process on the port.
    Escalates from SIGTERM to SIGKILL only after timeout.
    """
    pid, verified = get_server_pid(port)
    if pid is None:
        return {"ok": True, "already_stopped": True, "port": port, "message": f"No server running on port {port}."}

    if not verified:
        cmd = get_process_command(pid)
        return {
            "ok": False,
            "error": "port_conflict_unrelated_process",
            "message": f"Port {port} is occupied by an unrelated process (PID: {pid}, cmd: '{cmd[:60]}'). Refusing to kill.",
            "pid": pid,
            "port": port,
        }

    # Verified Strata server: terminate gracefully
    try:
        # If possible, signal process group so engine children terminate too
        try:
            pgid = os.getpgid(pid)
            if pgid != os.getpgrp():
                os.killpg(pgid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except OSError:
            os.kill(pid, signal.SIGTERM)

        # Wait for graceful shutdown
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cur_pid, _ = get_server_pid(port)
            if cur_pid is None:
                break
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.5)

        # Force kill if still lingering
        still_pid, _ = get_server_pid(port)
        if still_pid is not None:
            try:
                try:
                    pgid = os.getpgid(still_pid)
                    if pgid != os.getpgrp():
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        os.kill(still_pid, signal.SIGKILL)
                except OSError:
                    os.kill(still_pid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(1.0)

        # Confirm port is free
        final_pid, _ = get_server_pid(port)
        if final_pid is not None:
            return {
                "ok": False,
                "error": "shutdown_failed",
                "message": f"Server process {final_pid} could not be stopped.",
                "pid": final_pid,
            }

        return {
            "ok": True,
            "stopped_pid": pid,
            "port": port,
            "message": f"Stopped Strata server (PID: {pid}) on port {port}.",
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "message": f"Failed to stop server: {e}"}


# -----------------------------------------------------------------------------
# Status and Health Inspection
# -----------------------------------------------------------------------------

def resolve_api_key(repo_root: Path | None = None) -> str:
    """Resolve the API key using deterministic environment and configuration fallback."""
    # 1. Environment variables
    for var in ("STRATA_KEY", "STRATA_API_KEY"):
        val = os.environ.get(var)
        if val and val.strip():
            return val.strip()

    # 2. Check ~/.zshrc for STRATA_KEY export
    zshrc = Path.home() / ".zshrc"
    if zshrc.is_file():
        try:
            text = zshrc.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                if line.strip().startswith("export STRATA_KEY="):
                    part = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if part:
                        return part
        except Exception:
            pass

    # 3. Check local JSON configs
    root = repo_root or get_repo_root()
    for cfg_name in ("strata-iq3_s-512k.json", "strata-iq3_s.json"):
        cfg_path = root / cfg_name
        if cfg_path.is_file():
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                key = data.get("api_key")
                if key and isinstance(key, str) and key.strip():
                    return key.strip()
            except Exception:
                pass

    return ""


def get_status(port: int = DEFAULT_PORT, repo_root: Path | None = None) -> dict[str, Any]:
    """Inspect the status of the local server.

    Returns structured state:
      state: 'ready' | 'starting' | 'stopped' | 'auth_required' | 'unrelated_port_conflict' | 'error'
    """
    root = repo_root or get_repo_root()
    pid, verified = get_server_pid(port)

    if pid is None:
        return {
            "state": "stopped",
            "pid": None,
            "port": port,
            "model": None,
            "profile": None,
            "message": f"Strata server is NOT running (port {port} is idle).",
        }

    if not verified:
        cmd = get_process_command(pid)
        return {
            "state": "unrelated_port_conflict",
            "pid": pid,
            "port": port,
            "model": None,
            "profile": None,
            "message": f"Port {port} is occupied by an unrelated process (PID: {pid}).",
            "cmd": cmd,
        }

    key = resolve_api_key(root)
    url = f"http://127.0.0.1:{port}"

    # Try probing /v1/models or /metrics
    headers = {"User-Agent": "Strata-Manager/1.0", "Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    req = urllib.request.Request(f"{url}/v1/models", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            model_id = None
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], list) and data["data"]:
                model_id = data["data"][0].get("id")

            # Determine profile
            matched_profile = None
            if model_id:
                for pid_key, pdata in PROFILES.items():
                    info = inspect_profile(pid_key, root)
                    if info["model_name"] == model_id:
                        matched_profile = pid_key
                        break

            return {
                "state": "ready",
                "pid": pid,
                "port": port,
                "model": model_id,
                "profile": matched_profile,
                "data": data,
                "message": f"Strata server is RUNNING and READY (PID: {pid} on port {port}). Active model: {model_id or 'unknown'}",
            }
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {
                "state": "auth_required",
                "pid": pid,
                "port": port,
                "model": None,
                "profile": None,
                "message": f"Strata server is RUNNING (PID: {pid} on port {port}), but requires API key authentication.",
            }
        return {
            "state": "error",
            "pid": pid,
            "port": port,
            "model": None,
            "profile": None,
            "message": f"Strata server responded with HTTP {e.code}.",
        }
    except (urllib.error.URLError, TimeoutError, OSError):
        # Process is alive but not answering HTTP yet -> initializing
        return {
            "state": "starting",
            "pid": pid,
            "port": port,
            "model": None,
            "profile": None,
            "message": f"Strata server is RUNNING (PID: {pid} on port {port}), currently initializing weights...",
        }


# -----------------------------------------------------------------------------
# Launch and Switch Operations
# -----------------------------------------------------------------------------

def start_model(
    profile_name: str,
    background: bool = True,
    wait_ready: bool = True,
    timeout: float = 90.0,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Start or switch to a specified model profile.

    Stops any previously active server cleanly first.
    Sets necessary Apple Silicon / Metal runtime environment variables.
    """
    root = repo_root or get_repo_root()
    cid = canonical_profile_id(profile_name)
    if not cid:
        return {"ok": False, "error": f"Unknown model profile '{profile_name}'. Choose 'swift' or 'qwen'."}

    profile = inspect_profile(cid, root)
    port = profile["port"]

    # Check current status
    cur_status = get_status(port, root)
    if cur_status["state"] == "ready" and cur_status.get("profile") == cid:
        return {
            "ok": True,
            "already_running": True,
            "pid": cur_status["pid"],
            "port": port,
            "profile": cid,
            "message": f"{profile['display_name']} is already running and ready on port {port} (PID: {cur_status['pid']}).",
        }

    # Stop active server if any
    if cur_status["state"] in ("ready", "starting", "auth_required"):
        stop_res = stop_server(port)
        if not stop_res.get("ok"):
            return stop_res

    # Prepare environment
    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = "14"
    env["OPENBLAS_NUM_THREADS"] = "14"
    env["STRATA_PF_FUSED"] = "1"

    key = resolve_api_key(root)
    if key:
        env["STRATA_KEY"] = key
        env["STRATA_API_KEY"] = key

    log_path = root / "strata-server.log"
    py_bin = find_python()

    # Prefer launch script if executable, else execute server.py directly
    script = root / profile["launch_script"]
    if script.is_file() and os.access(script, os.X_OK):
        cmd = [str(script)]
    else:
        cmd = [
            py_bin,
            str(root / "serve" / "server.py"),
            "--engine",
            "strata",
            "--config",
            profile["config_file"],
            "--port",
            str(port),
        ]

    if background:
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"\n--- Launching {profile['display_name']} at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            log_file.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=str(root),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        pid = proc.pid

        if not wait_ready:
            return {
                "ok": True,
                "pid": pid,
                "port": port,
                "profile": cid,
                "state": "starting",
                "message": f"Started {profile['display_name']} in background (PID: {pid}).",
            }

        # Poll readiness
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout:
            if proc.poll() is not None:
                # Process exited prematurely
                err_tail = ""
                try:
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                        err_tail = "".join(lines[-15:])
                except Exception:
                    pass
                return {
                    "ok": False,
                    "error": "server_exited_prematurely",
                    "exit_code": proc.returncode,
                    "message": f"Server process exited with code {proc.returncode}.\nLog tail:\n{err_tail}",
                }

            st = get_status(port, root)
            if st["state"] == "ready":
                return {
                    "ok": True,
                    "pid": pid,
                    "port": port,
                    "profile": cid,
                    "state": "ready",
                    "message": f"{profile['display_name']} is ready on port {port} (PID: {pid}).",
                }
            time.sleep(1.5)

        return {
            "ok": True,
            "pid": pid,
            "port": port,
            "profile": cid,
            "state": "starting",
            "message": f"Server started (PID: {pid}) but did not complete loading within {timeout}s. Check {log_path}.",
        }
    else:
        # Foreground execution
        os.execvpe(cmd[0], cmd, env)


# -----------------------------------------------------------------------------
# API Key Management
# -----------------------------------------------------------------------------

def mask_key(key: str | None) -> str:
    """Format key securely without printing secret bytes."""
    if not key or not isinstance(key, str):
        return "none"
    s = key.strip()
    if not s:
        return "none"
    if len(s) <= 8:
        return f"*** ({len(s)} chars)"
    return f"{s[:4]}...{s[-4:]} ({len(s)} chars)"


def get_key_status(port: int = DEFAULT_PORT, repo_root: Path | None = None) -> dict[str, Any]:
    """Inspect authoritative key configuration and verify against active server.

    Never exposes secret in results.
    """
    root = repo_root or get_repo_root()
    env_strata_key = os.environ.get("STRATA_KEY", "").strip()
    env_api_key = os.environ.get("STRATA_API_KEY", "").strip()

    config_keys: dict[str, str] = {}
    for cfg_name in ("strata-iq3_s-512k.json", "strata-iq3_s.json"):
        cfg_p = root / cfg_name
        if cfg_p.is_file():
            try:
                d = json.loads(cfg_p.read_text(encoding="utf-8"))
                val = d.get("api_key", "")
                if val:
                    config_keys[cfg_name] = str(val).strip()
            except Exception:
                pass

    effective = resolve_api_key(root)

    # Test against running server
    server_verified = None
    pid, verified = get_server_pid(port)
    if pid is not None and verified:
        headers = {"Accept": "application/json"}
        if effective:
            headers["Authorization"] = f"Bearer {effective}"
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/status", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=1.5):
                server_verified = True
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                server_verified = False
            else:
                server_verified = True
        except Exception:
            server_verified = None

    return {
        "ok": True,
        "env_STRATA_KEY": mask_key(env_strata_key),
        "env_STRATA_API_KEY": mask_key(env_api_key),
        "config_keys": {k: mask_key(v) for k, v in config_keys.items()},
        "effective_masked": mask_key(effective),
        "server_running": pid is not None and verified,
        "server_pid": pid,
        "server_authenticated": server_verified,
        "requires_restart": (server_verified is False),
    }


def set_api_key(new_key: str, repo_root: Path | None = None) -> dict[str, Any]:
    """Set or rotate the API key across model configuration files."""
    root = repo_root or get_repo_root()
    clean_key = new_key.strip()
    if not clean_key or len(clean_key) < 8:
        return {"ok": False, "error": "Key must be at least 8 characters long."}

    updated = []
    for cfg_name in ("strata-iq3_s-512k.json", "strata-iq3_s.json"):
        cfg_p = root / cfg_name
        if cfg_p.is_file():
            try:
                data = json.loads(cfg_p.read_text(encoding="utf-8"))
                data["api_key"] = clean_key
                temp = cfg_p.with_suffix(".tmp")
                temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                temp.replace(cfg_p)
                updated.append(cfg_name)
            except Exception as e:
                return {"ok": False, "error": f"Failed updating {cfg_name}: {e}"}

    pid, verified = get_server_pid(DEFAULT_PORT)
    server_running = pid is not None and verified

    return {
        "ok": True,
        "updated_files": updated,
        "masked_key": mask_key(clean_key),
        "server_running": server_running,
        "server_pid": pid,
        "message": (
            f"API key updated in {', '.join(updated)} ({mask_key(clean_key)}).\n"
            + (
                f"Note: Running server (PID {pid}) still holds the previous key in memory; run 'strata switch restart' to adopt changes.\n"
                if server_running
                else ""
            )
            + "To ensure your terminal session matches, set: export STRATA_KEY=\"<key>\" in ~/.zshrc."
        ),
    }


def rotate_api_key(repo_root: Path | None = None) -> dict[str, Any]:
    """Generate a high-entropy random key and update configs."""
    new_key = secrets.token_hex(32)
    return set_api_key(new_key, repo_root)


# -----------------------------------------------------------------------------
# Interactive Menu
# -----------------------------------------------------------------------------

def interactive_menu(repo_root: Path | None = None) -> int:
    """Interactive model switcher and management menu for terminal users."""
    root = repo_root or get_repo_root()
    while True:
        os.system("clear" if os.name == "posix" else "cls")
        st = get_status(DEFAULT_PORT, root)

        print("==================================================")
        print(" Strata Model Manager (Apple Silicon Metal)")
        print("==================================================")
        if st["state"] == "ready":
            print(f"Status: RUNNING & READY (PID: {st['pid']} on port {st['port']})")
            print(f"Active: {st.get('model') or 'Unknown'}")
        elif st["state"] == "starting":
            print(f"Status: STARTING / LOADING (PID: {st['pid']} on port {st['port']})")
        elif st["state"] == "auth_required":
            print(f"Status: RUNNING (PID: {st['pid']}) — Requires API key auth")
        elif st["state"] == "unrelated_port_conflict":
            print(f"Status: PORT CONFLICT (Port {st['port']} used by PID {st['pid']})")
        else:
            print("Status: STOPPED (port 8000 is idle)")

        print("--------------------------------------------------")
        print("Select an option:")
        print("  1) Start Swift 1.5 Abliterated IQ3_S (512k, vision)")
        print("  2) Start Qwen 3.8 Flash Next IQ3_S (512k, vision)")
        print("  3) Show status & health probe")
        print("  4) Stop active server")
        print("  5) Launch live TUI monitor (strata monitor)")
        print("  6) API key configuration & status")
        print("  q) Quit to shell")
        print("--------------------------------------------------")

        try:
            choice = input("Choice [1-6, q]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            return 0

        if choice in ("q", "quit", "exit"):
            return 0
        elif choice == "1":
            print("\nStarting Swift 1.5 Abliterated IQ3_S...")
            res = start_model("swift", background=True, wait_ready=True, repo_root=root)
            print(res.get("message", ""))
            input("\nPress Enter to continue...")
        elif choice == "2":
            print("\nStarting Qwen 3.8 Flash Next IQ3_S...")
            res = start_model("qwen", background=True, wait_ready=True, repo_root=root)
            print(res.get("message", ""))
            input("\nPress Enter to continue...")
        elif choice == "3":
            print(f"\n{st.get('message', '')}")
            input("\nPress Enter to continue...")
        elif choice == "4":
            print("\nStopping server...")
            res = stop_server(DEFAULT_PORT)
            print(res.get("message", ""))
            input("\nPress Enter to continue...")
        elif choice == "5":
            from strata_cli import monitor_commands
            print("\nLaunching monitor...")
            ap_args = ["monitor"]
            try:
                monitor_commands.main(ap_args)
            except Exception as e:
                print(f"Monitor exited: {e}")
            input("\nPress Enter to return to menu...")
        elif choice == "6":
            ks = get_key_status(DEFAULT_PORT, root)
            print("\n--- API Key Status ---")
            print(f"Effective Key:     {ks['effective_masked']}")
            print(f"STRATA_KEY env:    {ks['env_STRATA_KEY']}")
            print(f"STRATA_API_KEY env:{ks['env_STRATA_API_KEY']}")
            for f, m in ks.get("config_keys", {}).items():
                print(f"Config {f}: {m}")
            if ks["server_running"]:
                auth_str = "Verified matching" if ks["server_authenticated"] else "Auth mismatch (restart needed)"
                print(f"Server (PID {ks['server_pid']}): {auth_str}")
            input("\nPress Enter to continue...")
        else:
            print("Invalid choice.")
            time.sleep(1.0)
