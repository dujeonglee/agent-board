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


def test_stop_instance_wait_returns_after_pid_gone(tmp_path):
    # v1.24.0 삭제-레이스 봉합: wait_s>0 이면 SIGTERM 후 프로세스가 실제로
    # 사라진 뒤에 반환 — 호출자(delete_post)가 곧바로 rmtree 해도 죽어가는
    # 인스턴스의 마지막 파일 쓰기가 지워진 경로를 재생성할 수 없다.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_web_json(tmp_path / "ws", "S1", proc.pid)
        stopped = instances.stop_instance(tmp_path / "ws", "S1", wait_s=8.0)
        assert stopped is True
        # 반환 시점에 이미 소멸 — 폴링 없이 즉시 죽어 있어야 한다.
        assert not instances.pid_alive(proc.pid)
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def test_stop_instance_wait_escalates_to_sigkill(tmp_path):
    # SIGTERM 을 무시하는 인스턴스(행/트랩)도 유한 대기 후 SIGKILL 로 정리.
    code = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('armed', flush=True); "
        "time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
    )
    try:
        proc.stdout.readline()  # TERM 핸들러 장착 확인 후 진행 (레이스 방지)
        _write_web_json(tmp_path / "ws", "S1", proc.pid)
        stopped = instances.stop_instance(tmp_path / "ws", "S1", wait_s=1.0)
        assert stopped is True
        assert not instances.pid_alive(proc.pid)  # SIGKILL 에스컬레이션
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
