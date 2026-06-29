"""Instance helpers — spawn command, free port, web.json discovery, liveness.

The pieces that don't need a real agent-cli process are unit-tested here:
- ``build_spawn_cmd`` (pure)
- ``pick_free_port`` (real socket)
- ``pid_alive`` (own pid vs a dead one)
- ``read_web_json`` / ``discover_session_id_by_pid`` (fixture web.json files)

``spawn`` / ``await_ready`` (which start agent-cli) are exercised in higher-level
integration once agent-cli is on PATH; their command/discovery logic is covered
by the units above.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent_board import instances
from agent_board.config import Config
from agent_board.models import Post


def _cfg(tmp_path) -> Config:
    return Config(data_dir=tmp_path / "data", workspaces_root=tmp_path / "ws")


def _write_web_json(workspace: Path, session_id: str, **fields):
    d = workspace / ".agent-cli" / "sessions" / session_id
    d.mkdir(parents=True, exist_ok=True)
    info = {
        "session_id": session_id,
        "host": "127.0.0.1",
        "port": 50001,
        "token": "tok",
        "pid": 111,
    }
    info.update(fields)
    (d / "web.json").write_text(json.dumps(info))
    return d / "web.json"


class TestBuildSpawnCmd:
    def test_new_session_has_no_resume(self, tmp_path):
        cfg = _cfg(tmp_path)
        post = Post(post_id="p1", topic="t")
        cmd = instances.build_spawn_cmd(cfg, post, port=50010, token="abc")
        assert "--resume" not in cmd
        # required orchestration flags
        for flag in (
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            "50010",
            "--token",
            "abc",
            "--no-browser",
            "--trust-local",
            "--idle-timeout",
            "--base-path",
            "/s/p1",
        ):
            assert flag in cmd, flag
        assert cmd[0] == cfg.agent_cli_bin

    def test_resume_when_session_known(self, tmp_path):
        cfg = _cfg(tmp_path)
        post = Post(post_id="p1", topic="t", session_id="S9")
        cmd = instances.build_spawn_cmd(cfg, post, port=50010, token="abc")
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == "S9"

    def test_idle_timeout_value(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.idle_timeout = 42
        cmd = instances.build_spawn_cmd(cfg, Post("p1", "t"), port=1, token="x")
        assert cmd[cmd.index("--idle-timeout") + 1] == "42"

    def test_model_flag_when_set(self, tmp_path):
        post = Post(post_id="p1", topic="t", model_id="Qwen3.6-27B")
        cmd = instances.build_spawn_cmd(_cfg(tmp_path), post, port=1, token="x")
        assert cmd[cmd.index("--model") + 1] == "Qwen3.6-27B"

    def test_no_model_flag_when_unset(self, tmp_path):
        cmd = instances.build_spawn_cmd(
            _cfg(tmp_path), Post("p1", "t"), port=1, token="x"
        )
        assert "--model" not in cmd  # provider default


class TestPickFreePort:
    def test_returns_bindable_port_in_range(self):
        import socket

        port = instances.pick_free_port(50000, 60000)
        assert 50000 <= port <= 60000
        # actually bindable
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.close()


class TestPidAlive:
    def test_own_pid_alive(self):
        assert instances.pid_alive(os.getpid()) is True

    def test_unused_pid_dead(self):
        assert instances.pid_alive(2_000_000_000) is False


class TestWebJsonDiscovery:
    def test_read_web_json(self, tmp_path):
        ws = tmp_path / "ws"
        _write_web_json(ws, "S1", port=50005, pid=222)
        info = instances.read_web_json(ws, "S1")
        assert info["session_id"] == "S1" and info["port"] == 50005

    def test_read_web_json_missing(self, tmp_path):
        assert instances.read_web_json(tmp_path / "ws", "nope") is None

    def test_discover_session_id_by_pid(self, tmp_path):
        ws = tmp_path / "ws"
        _write_web_json(ws, "OLD", pid=999)  # stale session, different pid
        _write_web_json(ws, "NEW", pid=12345)  # the one we just spawned
        assert instances.discover_session_id_by_pid(ws, 12345) == "NEW"

    def test_discover_returns_none_when_no_pid_match(self, tmp_path):
        ws = tmp_path / "ws"
        _write_web_json(ws, "OLD", pid=999)
        assert instances.discover_session_id_by_pid(ws, 12345) is None


class TestSpawnNonInteractive:
    def test_spawn_detaches_stdin(self, tmp_path, monkeypatch):
        # stdin=DEVNULL so agent-cli never blocks on a "Resume? [y/N]" prompt
        # (would otherwise hang the instance → await_ready timeout → /open 500).
        import subprocess

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kw):
                captured.update(kw)
                self.pid = 4321

        monkeypatch.setattr(instances.subprocess, "Popen", _FakePopen)
        instances.spawn(_cfg(tmp_path), Post("p1", "t"), port=50001, token="x")
        assert captured["stdin"] is subprocess.DEVNULL
        assert captured["start_new_session"] is True
