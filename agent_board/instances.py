"""agent-cli instance lifecycle helpers (DESIGN §5/§6).

The board spawns agent-cli web instances bound to loopback, on a board-chosen
port, and discovers the session_id from the instance's web.json by matching the
spawned process's pid. Liveness = pid alive + /api/health 200.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx

from agent_board.config import Config
from agent_board.models import Post


def build_spawn_cmd(config: Config, post: Post, *, port: int, token: str) -> list[str]:
    """The ``agent-cli web ...`` argv for this post. ``--resume`` only when the
    session already exists (first open creates a new session)."""
    cmd = [
        config.agent_cli_bin,
        "web",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--token",
        token,
        "--no-browser",
        "--trust-local",
        "--idle-timeout",
        str(config.idle_timeout),
        "--base-path",
        f"/s/{post.post_id}",
    ]
    if post.model_id:
        # agent-cli resolves the full definition (provider/url/key) from its own
        # registry; the board only passes the chosen id.
        cmd += ["--model", post.model_id]
    if post.session_id:
        cmd += ["--resume", post.session_id]
    return cmd


def pick_free_port(low: int, high: int) -> int:
    """An OS-assigned free port. The range is advisory — we let the OS pick a
    free ephemeral port and just sanity-check it falls in range, retrying."""
    for _ in range(50):
        s = socket.socket()
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        finally:
            s.close()
        if low <= port <= high:
            return port
    # fall back to an explicit scan if the ephemeral range sits outside [low,high]
    for port in range(low, high + 1):
        s = socket.socket()
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError(f"no free port in [{low}, {high}]")


def pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` exists (signal 0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def stop_instance(workspace: Path, session_id: str | None) -> bool:
    """Terminate a post's running instance (SIGTERM by pid from web.json).
    Returns True if a live instance was signalled. Called BEFORE removing the
    workspace so the instance isn't orphaned with a deleted cwd."""
    if not session_id:
        return False
    info = read_web_json(workspace, session_id)
    if not info:
        return False
    pid = info.get("pid")
    if pid and pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        return True
    return False


def _session_dir(workspace: Path, session_id: str) -> Path:
    return Path(workspace) / ".agent-cli" / "sessions" / session_id


def read_web_json(workspace: Path, session_id: str) -> dict | None:
    """The instance file for a known session, or None if absent/corrupt."""
    p = _session_dir(workspace, session_id) / "web.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def read_status_json(workspace: Path, session_id: str) -> dict | None:
    """The instance's live status sidecar (``{busy, awaiting_input, viewers}``),
    or None if absent/corrupt. agent-cli >= 4.27.0 writes it on every
    viewer/busy/awaiting change, so the board reads a local file instead of
    polling ``GET /api/health`` (older instances have no file → caller falls
    back to health)."""
    p = _session_dir(workspace, session_id) / "status.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def discover_session_id_by_pid(workspace: Path, pid: int) -> str | None:
    """After a fresh spawn (new session), find the session_id by matching the
    web.json whose ``pid`` equals the spawned child pid — robust against stale
    web.json files from earlier sessions in the same workspace."""
    base = Path(workspace) / ".agent-cli" / "sessions"
    if not base.is_dir():
        return None
    for wj in base.glob("*/web.json"):
        try:
            info = json.loads(wj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if info.get("pid") == pid:
            return info.get("session_id")
    return None


def health_info(port: int, *, timeout: float = 1.0) -> dict | None:
    """The instance's /api/health body (``{status, busy}``), or None if it
    doesn't answer 200 on loopback."""
    try:
        # trust_env=False: never route this loopback call through a corporate
        # HTTP proxy (HTTP_PROXY env) — the proxy returns "Access Denied" for
        # 127.0.0.1, which made health checks fail → await_ready timeout → 500.
        r = httpx.get(
            f"http://127.0.0.1:{port}/api/health", timeout=timeout, trust_env=False
        )
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


def health(port: int, *, timeout: float = 1.0) -> bool:
    """Whether the instance answers /api/health 200 on loopback."""
    return health_info(port, timeout=timeout) is not None


def alive(info: dict) -> bool:
    """An instance is alive iff its pid is running AND it answers health."""
    pid = info.get("pid")
    port = info.get("port")
    return bool(pid and pid_alive(pid) and port and health(port))


def spawn(config: Config, post: Post, *, port: int, token: str) -> subprocess.Popen:
    """Start the agent-cli web instance for this post (cwd = its workspace)."""
    workspace = config.workspace_for(post.post_id)
    workspace.mkdir(parents=True, exist_ok=True)
    cmd = build_spawn_cmd(config, post, port=port, token=token)
    # start_new_session: the instance runs in its own session/process group, so
    # it is independent of the board — it self-reaps on idle (--idle-timeout),
    # survives a board restart (re-attached via web.json), and a board Ctrl+C
    # does not abruptly SIGINT it. Explicit teardown goes through stop_instance.
    #
    # stdin=DEVNULL: spawn NON-interactively. Otherwise the child inherits the
    # board's terminal stdin, agent-cli sees a TTY, and (when a prior session
    # exists in the workspace) it BLOCKS on a "Resume it? [y/N]" prompt — the
    # server never starts, web.json is never written, await_ready times out and
    # /open returns 500. A null stdin makes agent-cli start fresh deterministically.
    #
    # stdout/stderr → a per-workspace log file instead of the board console: the
    # instance's startup banner (UI/Token/Session) would otherwise clutter the
    # board's terminal. Kept on disk so it's still there to debug.
    log_path = workspace / ".agent-cli" / "instance.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = log_path.open("a", encoding="utf-8")
    try:
        return subprocess.Popen(
            cmd,
            cwd=str(workspace),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
        )
    finally:
        logf.close()  # the child has its own dup of the fd; the parent's isn't needed


def await_ready(
    workspace: Path, pid: int, port: int, *, timeout: float = 20.0
) -> str | None:
    """Poll until the instance is ready, returning its session_id (discovered
    by pid). None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sid = discover_session_id_by_pid(workspace, pid)
        if sid and health(port):
            return sid
        time.sleep(0.25)
    return None
