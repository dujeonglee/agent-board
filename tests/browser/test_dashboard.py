"""board 대시보드 실브라우저 계약 — 에이전트 칩 + 탭 가드.

둘 다 실 브라우저 없이는 검증 불가: 에이전트 칩은 상태 라벨/색 렌더링,
탭 가드는 여러 실 탭의 BroadcastChannel 카운트.
"""

from __future__ import annotations

import time


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


class TestAgentsChip:
    def test_chip_and_agents_busy_status(self, board, browser):
        board.seed_post(
            topic="gomoku",
            agents={
                "alive": 2,
                "working": 1,
                "list": [
                    {"key": "a", "profile": "coder", "name": "ui", "state": "busy"},
                    {"key": "b", "profile": "rev", "name": "", "state": "idle"},
                ],
            },
        )
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector(".agents-chip", timeout=8000)
        assert page.inner_text(".agents-chip").strip() == "🤖 1/2"
        # main 유휴 + 에이전트 작업 중 = 보라 dot + "에이전트 작업 중" 라벨
        assert page.locator(".dot.agents-busy").count() == 1
        assert "에이전트 작업" in page.inner_text(".post .st")
        # hover 상세 (title 속성)
        assert "coder · ui: busy" in (page.get_attribute(".agents-chip", "title") or "")
        ctx.close()

    def test_no_chip_when_no_agents(self, board, browser):
        board.seed_post(topic="plain")  # agents 필드 없음
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector(".post", timeout=8000)
        page.wait_for_timeout(500)
        assert page.locator(".agents-chip").count() == 0
        ctx.close()


class TestTabGuard:
    def test_open_blocked_when_five_tabs_hold_connections(self, board, browser):
        """대시보드 탭 5개가 연결을 잡으면(각 /api/events SSE) 6번째 열기
        차단 — 실 브라우저의 BroadcastChannel 카운트 계약 (board-proxy)."""
        board.seed_post(topic="guard")
        ctx = browser.new_context()
        tabs = []
        for _ in range(5):
            t = ctx.new_page()
            t.goto(board.url, wait_until="load")
            tabs.append(t)

        clicker = tabs[0]
        open_posts = []
        clicker.on(
            "request",
            lambda r: r.url.endswith("/open") and open_posts.append(r.url),
        )
        clicker.wait_for_selector("button.open", timeout=8000)

        def _held():
            return clicker.evaluate(
                "new Promise(r=>{const ch=new BroadcastChannel("
                "'agentcli_tab_presence');const n=String(Date.now())+Math.random();"
                "let c=0;ch.addEventListener('message',e=>{const d=e.data||{};"
                "if(d.type==='pong'&&d.nonce===n&&d.held!==false)c++});"
                "ch.postMessage({type:'ping',nonce:n});"
                "setTimeout(()=>{ch.close();r(c)},200)})"
            )

        # 셋업 레이스 제거: 5탭 presence 응답기가 실제로 다 뜰 때까지 폴링.
        assert _wait(lambda: _held() >= 5, timeout=8)
        clicker.click("button.open")
        clicker.wait_for_timeout(700)
        toast = clicker.query_selector("#toast.show")
        blocked = toast and "연결 한도" in (toast.inner_text() or "")
        assert blocked and not open_posts, "6번째 열기가 차단돼야 한다"

        # 탭 정리 후 재클릭 → 게이트 통과. 닫힌 탭 presence 응답기가
        # 실제로 사라질 때까지(카운트 한도 아래) 폴링 — 고정 sleep 은
        # 브라우저 teardown 타이밍에 취약(실측: 500ms 부족).
        for t in tabs[2:]:
            t.close()
        assert _wait(lambda: _held() < 5, timeout=8)

        def _click_opens():
            if open_posts:
                return True
            clicker.click("button.open")
            clicker.wait_for_timeout(400)
            return bool(open_posts)

        assert _wait(_click_opens, timeout=8)
        ctx.close()
