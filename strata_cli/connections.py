"""Owner-local monitor connections; store references, never credential values.

This module is standard-library only so the portable monitor bundle and source
CLI resolve the same saved connection without importing server or installer code.
"""

from __future__ import annotations

try:
    from strata_control.private_files import private_mkdir as _private_mkdir
except ImportError:                                     # a thin bundle (monitor only) ships without strata_control
    def _private_mkdir(path, *, parents=False, exist_ok=True):
        path.mkdir(mode=0o700, parents=parents, exist_ok=exist_ok)
        return path
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from strata_cli.client import server_url, url_prefix


NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
MAX_BYTES = 4096
DEFAULT_URL = "http://127.0.0.1:8095"


class ConnectionError(ValueError):
    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


def _name(value: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ConnectionError("connection name must use lowercase a-z, 0-9, _ or - and start with a letter")
    return value


def prefix_path(value: str) -> str:
    try:
        return url_prefix(value)
    except ValueError as exc:
        raise ConnectionError(str(exc)) from None


def credential_name(value: str) -> str:
    if not isinstance(value, str) or not ENV_NAME.fullmatch(value):
        raise ConnectionError("credential reference must be an ASCII environment variable name")
    return value


def connection_dir(*, create: bool = False) -> Path:
    explicit = os.environ.get("STRATA_CONNECTION_DIR")
    if explicit:
        directory = Path(explicit).expanduser()
        if not directory.is_absolute():
            raise ConnectionError("STRATA_CONNECTION_DIR must be absolute")
    elif os.name == "nt":
        base = os.environ.get("APPDATA")
        directory = (Path(base) if base else Path.home() / "AppData" / "Roaming") / "Strata" / "connections"
    else:
        directory = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "strata" / "connections"
    if create:
        _private_mkdir(directory, parents=True, exist_ok=True)
    if directory.exists() or directory.is_symlink():
        details = directory.lstat()
        if not stat.S_ISDIR(details.st_mode) or (os.name == "posix" and
                (details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077)):
            raise ConnectionError("connection directory must be owner-only", 5)
    return directory


def _path(name: str, *, create_dir: bool = False) -> Path:
    return connection_dir(create=create_dir) / f"{_name(name)}.json"


def _validate(value: object, *, expected_name: str | None = None) -> dict:
    if not isinstance(value, dict) or set(value) != {"schema_version", "id", "url", "prefix", "credential_env"}:
        raise ConnectionError("connection has an unsupported schema", 5)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ConnectionError("connection has an unsupported schema version", 5)
    name = _name(value["id"])
    if expected_name is not None and name != expected_name:
        raise ConnectionError("connection ID does not match its filename", 5)
    try:
        url = server_url(value["url"])
    except (ValueError, TypeError):
        raise ConnectionError("connection URL must be a safe server root", 5) from None
    prefix = prefix_path(value["prefix"])
    credential = credential_name(value["credential_env"])
    if url != value["url"] or prefix != value["prefix"]:
        raise ConnectionError("connection URL or prefix is not canonical", 5)
    return {"schema_version": 1, "id": name, "url": url, "prefix": prefix,
            "credential_env": credential}


def _bytes(value: dict) -> bytes:
    data = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ConnectionError("connection record exceeds 4 KiB", 5)
    return data


def _revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def load(name: str) -> tuple[dict, str]:
    path = _path(name)
    try:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_BYTES or \
                (os.name == "posix" and (details.st_uid != os.getuid() or
                                         stat.S_IMODE(details.st_mode) & 0o077)):
            raise ConnectionError("connection file must be a private regular file under 4 KiB", 5)
        data = path.read_bytes()
        value = json.loads(data.decode("utf-8"))
    except FileNotFoundError:
        raise ConnectionError(f"connection {name!r} is not saved; use strata connection create", 5) from None
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, ConnectionError):
            raise
        raise ConnectionError("connection file is unreadable", 5) from None
    try:
        return _validate(value, expected_name=name), _revision(data)
    except (ConnectionError, ValueError, TypeError) as exc:
        raise ConnectionError(f"connection file has invalid values ({type(exc).__name__})", 5) from None


def list_connections() -> list[dict]:
    directory = connection_dir()
    if not directory.exists():
        return []
    paths = [path for path in sorted(directory.glob("*.json")) if NAME.fullmatch(path.stem)]
    if len(paths) > 100:
        raise ConnectionError("more than 100 connection records need inspection", 5)
    result = []
    for path in paths:
        try:
            value, revision = load(path.stem)
            result.append({"id": value["id"], "url": value["url"], "prefix": value["prefix"],
                           "credential_env": value["credential_env"], "revision": revision})
        except ConnectionError as exc:
            result.append({"id": path.stem, "error": str(exc)})
    return result


def _replace(path: Path, data: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name == "posix":
            try:
                parent = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            except OSError:
                # The replacement succeeded; do not report a failed save afterward.
                pass
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _lock(name: str):
    path = connection_dir(create=True) / f".{_name(name)}.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or (os.name == "posix" and
                (details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077)):
            raise ConnectionError("connection lock must be owner-only", 5)
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def create(name: str, url: str, *, prefix: str = "",
           credential_env: str = "STRATA_TELEMETRY_KEY") -> tuple[dict, str]:
    name = _name(name)
    value = _validate({"schema_version": 1, "id": name, "url": server_url(url),
                       "prefix": prefix_path(prefix), "credential_env": credential_name(credential_env)},
                      expected_name=name)
    data = _bytes(value)
    path = _path(name, create_dir=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise ConnectionError(f"connection {name!r} already exists") from None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return value, _revision(data)


def propose(name: str, assignment: str) -> tuple[dict, str]:
    current, revision = load(name)
    key, sep, raw = assignment.partition("=")
    if not sep or key not in ("url", "prefix", "credential_env"):
        raise ConnectionError("expected url=ROOT, prefix=/PATH or credential_env=ENV_NAME")
    candidate = dict(current)
    candidate[key] = (server_url(raw) if key == "url" else
                      prefix_path(raw) if key == "prefix" else credential_name(raw))
    return _validate(candidate, expected_name=name), revision


def save(name: str, value: dict, *, expected_revision: str) -> tuple[dict, str]:
    name = _name(name)
    value = _validate(value, expected_name=name)
    if not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{16}", expected_revision):
        raise ConnectionError("connection revision must be 16 lowercase hex characters")
    path = _path(name)
    with _lock(name):
        current, revision = load(name)
        if revision != expected_revision:
            raise ConnectionError("connection changed since preview; inspect it again", 6)
        data = _bytes(value)
        if value == current:
            return current, revision
        _replace(path, data)
        return value, _revision(data)


def probe_server(url: str, timeout: float = 0.5) -> dict:
    """Test whether a candidate URL is an active Strata server using standard library.

    Distinguishes:
    - active ready Strata server (status 200)
    - active Strata server requiring authentication (status 401/403 with Strata error structure)
    - Strata server in startup state (status 503)
    - unreachable or unrelated HTTP service
    """
    import urllib.request
    import urllib.error

    clean_url = url.rstrip("/")
    for probe_path in ("/v1/status", "/metrics", "/v1/models", "/health"):
        req = urllib.request.Request(
            f"{clean_url}{probe_path}",
            headers={"User-Agent": "Strata-Resolver/1.0", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=max(0.1, timeout)) as resp:
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read(4096)
                if "application/json" in content_type:
                    try:
                        data = json.loads(raw.decode("utf-8"))
                        if isinstance(data, (dict, list)):
                            return {"is_strata": True, "state": "ready", "status": resp.status, "data": data}
                    except Exception:
                        pass
                return {"is_strata": True, "state": "ready", "status": resp.status}
        except urllib.error.HTTPError as e:
            raw = e.read(2048) if hasattr(e, "read") else b""
            if e.code in (401, 403):
                try:
                    data = json.loads(raw.decode("utf-8"))
                    if isinstance(data, dict) and "error" in data:
                        return {"is_strata": True, "state": "auth_required", "status": e.code}
                except Exception:
                    pass
                return {"is_strata": True, "state": "auth_required", "status": e.code}
            elif e.code == 503:
                return {"is_strata": True, "state": "starting", "status": e.code}
            continue
        except Exception:
            break
    return {"is_strata": False, "state": "unreachable", "status": None}


def discover_local_instances(candidate_ports: tuple[int, ...] = (8000, 8095), timeout: float = 0.4) -> dict:
    """Scan candidate local ports for active Strata servers."""
    verified = []
    states = {}
    for p in candidate_ports:
        res = probe_server(f"http://127.0.0.1:{p}", timeout=timeout)
        if res.get("is_strata"):
            verified.append(p)
            states[p] = res.get("state")
    return {
        "verified_ports": verified,
        "ambiguity": len(verified) > 1,
        "states": states,
    }


def resolve(*, url: str | None = None, connection: str | None = None,
            credential_env: str | None = None, timeout: float | None = None,
            environment: dict | None = None) -> dict:
    env = os.environ if environment is None else environment
    selected = connection or (None if url is not None else env.get("STRATA_CONNECTION"))
    if url is not None and connection is not None:
        raise ConnectionError("choose --url or --connection, not both")

    ambiguity = False
    discovered_ports: list[int] = []

    if selected:
        saved, revision = load(selected)
        root, prefix = saved["url"], saved["prefix"]
        key_ref = saved["credential_env"]
        origins = {"url": "connection", "prefix": "connection", "credential_env": "connection"}
    else:
        revision = None
        prefix = ""
        if url is not None:
            root = url
            origins = {"url": "flag", "prefix": "default"}
        elif "STRATA_URL" in env:
            root = env["STRATA_URL"]
            origins = {"url": "environment", "prefix": "default"}
        else:
            discovery = discover_local_instances((8000, 8095), timeout=0.3)
            verified = discovery["verified_ports"]
            discovered_ports = verified
            if len(verified) == 1:
                root = f"http://127.0.0.1:{verified[0]}"
                origins = {"url": "local_discovery", "prefix": "default"}
            elif len(verified) > 1:
                ambiguity = True
                root = "http://127.0.0.1:8000"
                origins = {"url": "local_discovery_ambiguous", "prefix": "default"}
            else:
                has_local_8000 = any(
                    Path(p).is_file() for p in (
                        "strata-iq3_s-512k.json", "strata-iq3_s.json",
                        "/Volumes/vl/ai/strata/strata-iq3_s-512k.json",
                    )
                )
                root = "http://127.0.0.1:8000" if has_local_8000 else DEFAULT_URL
                origins = {"url": "default", "prefix": "default"}

        if "STRATA_KEY" in env and "STRATA_API_KEY" not in env:
            key_ref = "STRATA_KEY"
            origins["credential_env"] = "environment"
        elif "STRATA_API_KEY" in env and "STRATA_KEY" not in env:
            key_ref = "STRATA_API_KEY"
            origins["credential_env"] = "environment"
        elif "STRATA_KEY" in env and "STRATA_API_KEY" in env:
            key_ref = "STRATA_KEY" if (":8000" in root) else "STRATA_API_KEY"
            origins["credential_env"] = "environment"
        else:
            key_ref = "STRATA_KEY" if (":8000" in root) else "STRATA_API_KEY"
            origins["credential_env"] = "default"

    if credential_env is not None:
        key_ref = credential_env
        origins["credential_env"] = "flag"

    if timeout is not None and (type(timeout) not in (int, float) or not 0 < timeout <= 30):
        raise ConnectionError("timeout must be greater than zero and at most 30 seconds")
    origins["timeout_s"] = "flag" if timeout is not None else "default"

    normalized_url, normalized_prefix = server_url(root), prefix_path(prefix)
    has_key = bool(env.get(key_ref) or env.get("STRATA_KEY") or env.get("STRATA_API_KEY"))

    result = {
        "url": normalized_url,
        "prefix": normalized_prefix,
        "base_url": normalized_url + normalized_prefix,
        "credential_env": credential_name(key_ref),
        "credential_present": has_key,
        "timeout_s": timeout if timeout is not None else 3.0,
        "connection": selected,
        "connection_revision": revision,
        "origins": origins,
    }
    if ambiguity:
        result["ambiguity"] = True
        result["discovered_ports"] = discovered_ports
    return result
