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
        assert "Agents working" in page.inner_text(".post .st")
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


def _card(board, browser, **seed):
    """글 하나를 심고 그 카드의 상태 글·점·🤖 칩·툴팁을 읽는다."""
    board.seed_post(topic="w", **seed)
    page = browser.new_context().new_page()
    page.goto(board.url, wait_until="load")
    page.wait_for_selector(".post .st .dot", timeout=8000)
    chip = page.locator(".agents-chip")
    return {
        "st": page.inner_text(".post .st").split("👁")[0].strip(),
        "dot": page.get_attribute(".post .st .dot", "class"),
        "chip": chip.inner_text().strip() if chip.count() else "",
        "title": chip.get_attribute("title") if chip.count() else "",
    }


def _agent(name, *, waiting):
    return {
        "key": f"agt-{name}",
        "profile": "coder",
        "name": name,
        "state": "busy",
        "waiting": waiting,
    }


class TestWaiting:
    """main 이 제자리 대기(agent-cli ≥10.165.0 의 ``waiting``)·기다리는 에이전트(``agents.waiting``·``list[].waiting``).
    장면 값은 agent-cli 10.177.2 가 실제로 쓴 status.json 의 모양 그대로다(IMPL-display-a2 §1.2)."""

    def test_main_waiting_on_a_job_is_waiting(self, board, browser):
        """S4 — 작업만 기다리는 main: "Waiting"(깜박이지 않는 점). 종전 "Responding"."""
        c = _card(board, browser, status={"busy": True, "waiting": True})
        assert c["st"] == "Waiting" and c["dot"] == "dot waiting"

    def test_main_waiting_while_an_agent_works_is_agents_working(self, board, browser):
        """S3 — 에이전트의 회신을 기다리는 main, 에이전트는 일함: "Agents working"(D2-추가). 종전 "Responding"."""
        agents = {
            "alive": 1,
            "working": 1,
            "waiting": 0,
            "list": [_agent("ui", waiting=False)],
        }
        c = _card(board, browser, status={"busy": True, "waiting": True}, agents=agents)
        assert c["st"] == "Agents working" and c["dot"] == "dot agents-busy"
        assert c["chip"] == "🤖 1/1" and c["title"] == "coder · ui: busy"

    def test_an_agent_waiting_alone_is_idle_with_a_pause_mark(self, board, browser):
        """S9 — main 한가, 에이전트가 작업만 기다림: "Idle", 칩 `🤖 0/1 · ⏸ 1`, 툴팁 waiting (D2)."""
        agents = {
            "alive": 1,
            "working": 0,
            "waiting": 1,
            "list": [_agent("ui", waiting=True)],
        }
        c = _card(board, browser, agents=agents)
        assert c["st"] == "Idle" and c["chip"] == "🤖 0/1 · ⏸ 1"
        assert c["title"] == "coder · ui: waiting"

    def test_one_agent_waits_on_another_that_works(self, board, browser):
        """S10 — "Agents working", 칩 `🤖 1/2 · ⏸ 1`, 툴팁은 에이전트마다."""
        agents = {
            "alive": 2,
            "working": 1,
            "waiting": 1,
            "list": [_agent("aa", waiting=True), _agent("bb", waiting=False)],
        }
        c = _card(board, browser, agents=agents)
        assert c["st"] == "Agents working" and c["chip"] == "🤖 1/2 · ⏸ 1"
        assert c["title"] == "coder · aa: waiting\ncoder · bb: busy"

    def test_main_waits_on_an_agent_that_waits_too(self, board, browser):
        """S11 — 일하는 이가 없다: 규칙 그대로 "Waiting"(Q2). 종전 "Responding"."""
        agents = {
            "alive": 1,
            "working": 0,
            "waiting": 1,
            "list": [_agent("aa", waiting=True)],
        }
        c = _card(board, browser, status={"busy": True, "waiting": True}, agents=agents)
        assert c["st"] == "Waiting" and c["chip"] == "🤖 0/1 · ⏸ 1"

    def test_a_question_comes_first(self, board, browser):
        """S5 — 질문의 답을 기다리는 main: ❗ 가 1순위(Q1)."""
        c = _card(
            board,
            browser,
            status={"busy": True, "waiting": True, "awaiting_input": True},
        )
        assert c["st"] == "❗ Needs your reply"

    def test_an_old_room_reads_as_before(self, board, browser):
        """agent-cli < 10.165.0 — ``waiting`` 키가 없다: 작업 구간 중이면 "Responding", 칩은 ⏸ 없이 state."""
        agents = {
            "alive": 1,
            "working": 1,
            "list": [{"key": "a", "profile": "coder", "name": "ui", "state": "busy"}],
        }
        c = _card(board, browser, status={"busy": True}, agents=agents)
        assert c["st"] == "Responding" and c["dot"] == "dot busy"
        assert c["chip"] == "🤖 1/1" and c["title"] == "coder · ui: busy"


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
        blocked = toast and "Connection limit" in (toast.inner_text() or "")
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


class TestHeldFilterCounting:
    """countHeldTabs 의 held:false 제외 필터 (v1.14.1 수리) — 파킹 탭
    (held:false)은 열기 한도에서 빠지고, held 필드 없는 pong(구버전
    agent-cli 탭)은 보유로 집계. 합성 응답기를 주입해 결정적으로 검증."""

    def _install_responder(self, page, held_value):
        """페이지에 tab-presence ping 에 지정 held 로 답하는 응답기 N개
        효과를 내는 단일 리스너 설치 (여러 탭 흉내)."""
        page.evaluate(
            """(cfg) => {
              const ch = new BroadcastChannel('agentcli_tab_presence');
              ch.addEventListener('message', (e) => {
                const d = e.data || {};
                if (d.type === 'ping') {
                  for (let i = 0; i < cfg.n; i++) {
                    const pong = {type:'pong', nonce:d.nonce, path:'/x'+i};
                    // held: 실제 boolean (문자열 "false" 는 !== false 통과)
                    if (cfg.held === 'true') pong.held = true;
                    else if (cfg.held === 'false') pong.held = false;
                    // 'absent' → held 필드 자체를 넣지 않음
                    ch.postMessage(pong);
                  }
                }
              });
              window.__resp = ch;
            }""",
            {"n": held_value[1], "held": held_value[0]},
        )

    def _click_open_fires(self, page, post_id):
        """열기 클릭이 실제로 open POST 를 발화했는지 — 게이트가 열기
        전에 countHeldTabs 로 막으면 발화 안 됨. app.js 의 진짜 필터를
        게이트 경유로 검증(로직 재구현 아님 — 뮤테이션이 잡힘)."""
        fired = []
        page.on(
            "request",
            lambda r: r.url.endswith("/open") and fired.append(r.url),
        )
        page.click(f'.post[data-id="{post_id}"] button.open')
        page.wait_for_timeout(700)
        return bool(fired)

    def test_parked_held_false_excluded_from_open_gate(self, board, browser):
        """held:false(파킹) 응답기 6개를 주입해도 열기 게이트가 안 막힘 —
        실제 탭은 대시보드 1개(<5)뿐. 필터가 제거되면 6개가 세어져 차단
        → 발화 실패(뮤테이션 캐치)."""
        pid = board.seed_post(topic="guard")
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector(f'.post[data-id="{pid}"] button.open', timeout=8000)
        self._install_responder(page, ("false", 6))
        page.wait_for_timeout(300)
        assert self._click_open_fires(page, pid), (
            "held:false 6개가 한도 카운트에 포함되면 안 됨 (열기 발화해야)"
        )
        ctx.close()

    def test_held_absent_counted_blocks_open_gate(self, board, browser):
        """held 필드 없는 pong(구버전 탭) 5개 → 보유로 집계돼 열기 차단."""
        pid = board.seed_post(topic="guard")
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector(f'.post[data-id="{pid}"] button.open', timeout=8000)
        self._install_responder(page, ("absent", 5))
        page.wait_for_timeout(300)
        # absent 5 + 대시보드 1 = 6 ≥ 5 → 차단(발화 안 함) + 토스트
        assert not self._click_open_fires(page, pid)
        toast = page.query_selector("#toast.show")
        assert toast and "Connection limit" in (toast.inner_text() or "")
        ctx.close()


class TestTabTargeting:
    """열기 시 **탭 타깃 판정** (v1.29.0).

    종전엔 항상 named target(``agentcli-<post_id>``)으로 열었는데,
    ``window.name`` 은 사용자가 그 탭을 딴 주소로 옮겨도 남아 있어(같은
    origin 이면 브라우저가 유지) **사용자가 다른 용도로 쓰던 탭을 빼앗아**
    갔다 — 브라우저는 "이름표"만 볼 뿐 그 안에 룸이 떠 있는지 모른다.

    이제 presence pong(살아있는 agent-cli 룸 탭만 자기 path 로 답한다)으로
    판정한다: 룸 탭이 살아있으면 재사용, 없으면 새 탭(+이름 부여로 다음
    재사용은 유지).
    """

    def _fake_room_tab(self, page, post_id):
        """살아있는 룸 탭 흉내 — presence ping 에 그 글의 path 로 pong."""
        page.evaluate(
            "(pid)=>{const ch=new BroadcastChannel('agentcli_tab_presence');"
            "ch.addEventListener('message',e=>{const d=e.data||{};"
            "if(d.type==='ping')ch.postMessage({type:'pong',nonce:d.nonce,"
            "path:'/s/'+pid+'/',held:true});});}",
            post_id,
        )

    def _target_of_open_click(self, page):
        """열기 클릭이 window.open 에 넘긴 target 을 잡아 반환."""
        # 가짜 window 반환 — null 이면 코드가 location.href 폴백으로 페이지를
        # 이동시켜(문서 교체) 기록해둔 target 이 사라진다.
        page.evaluate(
            "window.__t=null;"
            "window.open=function(u,t){window.__t=t;"
            "return {name:'',focus:function(){}};};"
        )
        page.click("button.open")
        _wait(lambda: page.evaluate("window.__t") is not None, timeout=8)
        return page.evaluate("window.__t")

    def test_new_tab_when_no_live_room_tab(self, board, browser):
        """룸 탭이 없으면(사용자가 그 탭을 딴 주소로 옮겼거나 닫았거나)
        ``_blank`` — 즉 **새 탭**. 쓰던 탭을 빼앗지 않는다."""
        board.seed_post(topic="target")
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector("button.open", timeout=8000)

        assert self._target_of_open_click(page) == "_blank"
        ctx.close()

    def test_reuses_named_tab_when_room_is_live(self, board, browser):
        """룸을 실제로 띄운 탭이 살아있으면 named target 으로 재사용 —
        탭이 무한정 늘지 않는다(연결 한도 보호)."""
        post_id = board.seed_post(topic="target")
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector("button.open", timeout=8000)

        room = ctx.new_page()
        room.goto(board.url, wait_until="load")
        self._fake_room_tab(room, post_id)

        assert self._target_of_open_click(page) == "agentcli-" + post_id
        ctx.close()


class TestWorkspaceIdChip:
    """디스크 ↔ 카드 매핑 (v1.30.0).

    post_id 가 32자 uuid hex 에서 6자 랜덤으로 짧아지면서 워크스페이스 경로가
    크게 줄었지만, 짧은 랜덤 id 는 그 자체로는 어떤 글인지 말해주지 않는다.
    그래서 대시보드가 id 를 노출해야 `<workspaces_root>/<id>` 디렉토리를 보고
    어느 글인지 되짚을 수 있다 — 이게 없으면 이번 변경은 매핑 문제를 해결하지
    못하고 토큰만 줄인 것이 된다.
    """

    def test_card_shows_the_workspace_id(self, board, browser):
        pid = board.seed_post(topic="gomoku")
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector(".ws-id", timeout=8000)
        assert page.inner_text(".ws-id").strip() == pid
        ctx.close()

    def test_id_matches_the_directory_on_disk(self, board, browser):
        board.seed_post(topic="gomoku")
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector(".ws-id", timeout=8000)
        shown = page.inner_text(".ws-id").strip()
        assert (board.cfg.workspaces_root / shown).is_dir(), (
            "카드에 보이는 id 로 워크스페이스를 찾을 수 없다 — 매핑 실패"
        )
        ctx.close()

    def test_clicking_the_chip_flashes_copied(self, board, browser):
        board.seed_post(topic="gomoku")
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector(".ws-id", timeout=8000)
        page.click(".ws-id")
        assert _wait(lambda: "copied" in page.inner_text(".ws-id")), (
            f"복사 피드백 없음: {page.inner_text('.ws-id')!r}"
        )
        ctx.close()
