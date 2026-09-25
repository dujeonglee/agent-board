"""agent-cli instance lifecycle helpers (DESIGN §5/§6).

The board spawns agent-cli web instances bound to loopback, on a board-chosen
port, and discovers the session_id from the instance's web.json by matching the
spawned process's pid. Liveness = pid alive + /api/health 200.
"""

from __future__ import annotations

import functools
import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx

from agent_board.config import Config
from agent_board.models import Post


@functools.lru_cache(maxsize=8)
def cli_version(agent_cli_bin: str) -> str | None:
    """``<agent_cli_bin> --version`` 의 x.y.z — board 가 방을 spawn 하는
    agent-cli 바이너리 버전. 바이너리 부재/실패/미인식이면 None. lru_cache
    로 (bin 경로당) 프로세스 생애 1회만 실행 — 버전은 프로세스 중 안 바뀐다.
    ``--version`` 은 stdout/stderr 어디로든 나올 수 있어 둘 다 스캔."""
    try:
        out = subprocess.run(
            [agent_cli_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+\.\d+\.\d+)", (out.stdout or "") + (out.stderr or ""))
    return m.group(1) if m else None


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


def pick_free_port(
    low: int, high: int, *, exclude: frozenset[int] = frozenset()
) -> int:
    """An OS-assigned free port. The range is advisory — we let the OS pick a
    free ephemeral port and just sanity-check it falls in range, retrying.

    ``exclude`` (v1.31.0): ports the caller has handed out but whose instance
    has not bound yet. A bind probe only says "free *now*"; ``agent-cli web``
    takes seconds to reach its bind, and two opens inside that window got the
    same port (the second died with EADDRINUSE). The orchestrator keeps the
    in-flight set and passes it here."""
    for _ in range(50):
        s = socket.socket()
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        finally:
            s.close()
        if low <= port <= high and port not in exclude:
            return port
    # fall back to an explicit scan if the ephemeral range sits outside [low,high]
    for port in range(low, high + 1):
        if port in exclude:
            continue
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
    """Whether a process with ``pid`` is actually running.

    signal-0 프로브 + **좀비 판정** (v1.18.0): 인스턴스가 크래시/외부
    kill 로 죽으면 부모(board)가 wait 하지 않아 좀비로 잔존하는데,
    ``os.kill(pid, 0)`` 은 좀비도 성공한다 — 그러면 death edge 가 영영
    안 떠 caddy 모드의 죽은 라우트가 502 로 고착된다(board-proxy 는
    lazy revive 라 무증상). 좀비면 죽은 것으로 판정하고, 우리 자식이면
    reap 까지 해 프로세스 테이블에서 치운다."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        ).stdout.strip()
    except Exception:
        return True  # ps 실패 — 보수적으로 살아있다고 본다
    if out.startswith("Z"):
        try:
            os.waitpid(pid, os.WNOHANG)  # 우리 자식이면 reap (아니면 ECHILD)
        except (ChildProcessError, OSError):
            pass
        return False
    return bool(out)  # 빈 출력 = ps 가 못 찾음 (경계: kill 직후)


def stop_instance(
    workspace: Path, session_id: str | None, *, wait_s: float = 0.0
) -> bool:
    """Terminate a post's running instance (SIGTERM by pid from web.json).
    Returns True if a live instance was signalled. Called BEFORE removing the
    workspace so the instance isn't orphaned with a deleted cwd.

    ``wait_s`` > 0 — SIGTERM 후 프로세스가 **실제로 사라질 때까지** 유한
    대기(폴링), 타임아웃이면 SIGKILL 후 짧게 재대기 (v1.24.0). 삭제 경로의
    좀비-워크스페이스 레이스 봉합: signal-만-보내고-반환하던 종전 동작은
    rmtree 뒤에 죽어가는 인스턴스의 마지막 status 발행이 지워진 경로를
    재생성해, DB 행 없는 고아 디렉토리(내용물=status.json 하나)를 남겼다."""
    if not session_id:
        return False
    info = read_web_json(workspace, session_id)
    if not info:
        return False
    pid = info.get("pid")
    if not (pid and pid_alive(pid)):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    if wait_s > 0 and not _wait_pid_gone(pid, wait_s):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        _wait_pid_gone(pid, 2.0)
    return True


def _wait_pid_gone(pid: int, timeout_s: float, step_s: float = 0.1) -> bool:
    """``pid`` 가 사라질 때까지 폴링 — True=소멸, False=타임아웃.
    pid_alive 가 좀비를 죽음으로 판정+reap 하므로 여기서 따로 wait 안 한다."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(step_s)
    return not pid_alive(pid)


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


def inject_prompt(
    workspace: Path,
    session_id: str,
    prompt: str,
    *,
    nickname: str = "",
    timeout: float = 5.0,
) -> None:
    """Deliver a chat prompt to the post's RUNNING instance (schedules —
    docs/schedule-design.md §4). Reads ``web.json`` for port+token and POSTs
    ``/api/input`` on loopback; the instance queues it and injects at a turn
    boundary, so a busy agent is safe. ``nickname`` attributes the message
    (cli >= 8.9.0; older instances ignore the field and show '?'). Raises on
    any failure — the scheduler demotes the fire to a missed question."""
    wj = read_web_json(workspace, session_id)
    if not wj:
        raise RuntimeError("instance web.json missing — not running?")
    port, token = wj.get("port"), wj.get("token")
    body: dict = {"kind": "chat", "content": prompt}
    if nickname:
        body["nickname"] = nickname
    r = httpx.post(
        f"http://127.0.0.1:{port}/api/input?token={token}",
        json=body,
        timeout=timeout,
        trust_env=False,  # health_info 와 동일 — 회사 프록시 우회
    )
    r.raise_for_status()


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
            # ⏰ "외부 스케줄러가 이 워크스페이스를 본다"는 일반화된 신호
            # (board 특정 아님 — cli 는 board 를 모른다). cli >= 8.9.0 은 이
            # env 가 있을 때만 schedule 도구를 등록; 구버전은 그냥 무시.
            env={**os.environ, "AGENT_CLI_SCHEDULER": "1"},
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
