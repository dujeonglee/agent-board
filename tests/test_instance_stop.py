"""instances.stop_instance — terminate a post's running agent-cli instance.

On delete (and clean shutdown) the board must kill the spawned instance BEFORE
removing its workspace, else the instance is orphaned with a deleted cwd and
fails to save its session on exit.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from agent_board import instances


def _write_web_json(ws: Path, sid: str, pid: int, port: int = 50001):
    d = ws / ".agent-cli" / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "web.json").write_text(
        json.dumps({"session_id": sid, "port": port, "pid": pid, "token": "t"})
    )


def test_stop_instance_terminates_running_pid(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_web_json(tmp_path / "ws", "S1", proc.pid)
        assert instances.pid_alive(proc.pid)

        stopped = instances.stop_instance(tmp_path / "ws", "S1")
        assert stopped is True

        # wait() reaps the (now-terminating) child and confirms it exited —
        # pid_alive alone would see a not-yet-reaped zombie as "alive".
        proc.wait(timeout=5)
        assert proc.returncode is not None
    finally:
        proc.kill()


def test_stop_instance_no_web_json_is_noop(tmp_path):
    assert instances.stop_instance(tmp_path / "ws", "missing") is False


def test_stop_instance_none_session(tmp_path):
    assert instances.stop_instance(tmp_path / "ws", None) is False


def test_stop_instance_dead_pid_is_noop(tmp_path):
    _write_web_json(tmp_path / "ws", "S1", 2_000_000_000)  # never-used pid
    assert instances.stop_instance(tmp_path / "ws", "S1") is False
