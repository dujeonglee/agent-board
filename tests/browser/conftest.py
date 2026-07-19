"""agent-board 실브라우저 테스트 픽스처 (v1.19.0).

탭 가드(연결 보유 탭 카운트)·에이전트 상태 칩처럼 실 브라우저 + 실
board 서버 없이는 검증 못 하는 프런트 계약을 헤드리스 크롬으로 고정.
이번 주 스크래치(tab_guard_e2e·board_agents_e2e)의 승격.

옵트인: ``AGENT_BOARD_BROWSER_TESTS=1 pytest tests/browser/``. 그 외엔
루트 conftest 의 ``collect_ignore`` 로 수집조차 안 한다(cli 와 동일 —
per-item skip 은 pytest-asyncio 수집 단계 이벤트루프를 남긴다).
참고: 이 저장소는 아직 CI 가 없어 로컬/수동 실행 전용.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest

pytestmark = pytest.mark.browser


class _FakeOrch:
    """열기/재시작을 실제 spawn 없이 흉내 — 탭 가드는 열기 POST 발화
    여부만 보므로 spawn 기계를 격리한다 (test_app 의 FakeOrch 동형)."""

    async def open(self, post_id):
        return f"/s/{post_id}/"

    async def restart(self, post_id):
        return f"/s/{post_id}/?token=T"

    async def change_model(self, post_id, model_id):
        return {"ok": True, "changed": True}


class _FakeKeepalive:
    async def enable(self, post_id):
        pass

    async def disable(self, post_id):
        pass


@pytest.fixture(scope="session")
def browser():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


class BoardStack:
    """실 uvicorn 위의 board app(board-proxy 게이트웨이). 인스턴스 spawn
    은 하지 않고, 필요한 세션 사이드카(web.json/status.json)를 직접 심어
    live_state 를 원하는 상태로 만든다 — 프런트 렌더 계약만 검증."""

    def __init__(self, tmp_path):
        import uvicorn

        from agent_board.app import create_app
        from agent_board.config import Config
        from agent_board.store import Store

        self.cfg = Config(data_dir=tmp_path / "data", workspaces_root=tmp_path / "ws")
        self.store = Store(self.cfg.db_path)
        app = create_app(
            self.cfg,
            store=self.store,
            orchestrator=_FakeOrch(),
            keepalive=_FakeKeepalive(),
        )
        self._uv = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        )
        self._thread = threading.Thread(target=self._uv.run, daemon=True)

    def start(self) -> str:
        self._thread.start()
        deadline = time.time() + 10
        while not self._uv.started:
            if time.time() > deadline:
                raise RuntimeError("board uvicorn did not start")
            time.sleep(0.02)
        sock: socket.socket = self._uv.servers[0].sockets[0]
        self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        return self.url

    def stop(self):
        self._uv.should_exit = True
        self._thread.join(timeout=5)

    def seed_post(self, topic="t", *, agents=None, alive_pid=None):
        """게시글 + 라이브 인스턴스 사이드카. ``agents``=status.json 의
        상주 에이전트 요약, ``alive_pid``=pid_alive 통과용(기본 이 프로세스).
        반환: post_id."""
        post = self.store.create_post(topic=topic)
        sid = str(int_from(post.post_id))
        self.store.set_session_id(post.post_id, sid)
        sdir = self.cfg.workspace_for(post.post_id) / ".agent-cli" / "sessions" / sid
        sdir.mkdir(parents=True)
        pid = alive_pid if alive_pid is not None else os.getpid()
        (sdir / "web.json").write_text(
            json.dumps(
                {
                    "session_id": sid,
                    "host": "127.0.0.1",
                    "port": 1,
                    "token": "t",
                    "pid": pid,
                }
            )
        )
        status = {"busy": False, "awaiting_input": False, "viewers": 0}
        if agents is not None:
            status["agents"] = agents
        (sdir / "status.json").write_text(json.dumps(status))
        return post.post_id


def int_from(s: str) -> int:
    return int(s[:8], 16)


@pytest.fixture
def board(tmp_path):
    s = BoardStack(tmp_path)
    s.start()
    yield s
    s.stop()
