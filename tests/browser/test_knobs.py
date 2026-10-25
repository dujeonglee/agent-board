"""노브 거울 (v1.36.0) — 실브라우저 계약.

보드는 노브를 **새 글 폼 한 번** 받고, 그 뒤엔 방이 보고한 값을 읽기 전용으로
보여 준다. 여기서 고정하는 것: 폼이 표(/api/knobs/spec)에서 그려지고 제출값이
knobs 로 저장된다, 카드가 🤖 모델 칩과 기본과 다른 노브 칩을 그린다, ⚙ 패널은
읽기 전용이고 방이 보고한 값(status.json knobs)을 따른다, 복제 폼은 원본
값으로 채워진다, 표 없는 구버전 agent-cli 에선 ⚙ 고급이 없다.
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


def _page(browser, board):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(board.url, wait_until="load")
    page.wait_for_selector("#new-topic", timeout=8000)
    return ctx, page


class TestCreateForm:
    def test_advanced_form_is_drawn_from_the_spec(self, board, browser):
        ctx, page = _page(browser, board)
        page.wait_for_selector("#new-adv:not([hidden])", timeout=8000)
        assert page.locator("#new-knobs").is_hidden()
        page.click("#new-adv")
        assert page.locator("#new-knobs").is_visible()
        names = page.evaluate(
            "() => Array.from(document.querySelectorAll('#new-knobs [data-knob]')).map(e => e.dataset.knob)"
        )
        assert names == ["thinking", "max_agents", "stall", "auditor"]  # 모델은 제 자리
        # 종류별 컨트롤: choice/bool → select, number/duration → input
        tags = page.evaluate(
            "() => Array.from(document.querySelectorAll('#new-knobs [data-knob]')).map(e => e.tagName)"
        )
        assert tags == ["SELECT", "INPUT", "INPUT", "SELECT"]
        assert "min · 0 = off" in page.inner_text("#new-knobs")  # 표의 단위
        ctx.close()

    def test_submitted_knobs_are_stored_with_the_model(self, board, browser):
        ctx, page = _page(browser, board)
        page.wait_for_selector("#new-adv:not([hidden])", timeout=8000)
        page.click("#new-adv")
        page.fill("#new-topic", "노브 방")
        page.select_option("#new-knobs [data-knob=thinking]", "on")
        page.fill("#new-knobs [data-knob=max_agents]", "3")
        page.select_option("#new-knobs [data-knob=auditor]", "on")
        page.click("#new-create")
        assert _wait(lambda: len(board.store.list_posts()) == 1)
        post = board.store.list_posts()[0]
        assert post.knobs == {
            "model": "m",
            "thinking": "on",
            "max_agents": 3,
            "auditor": True,
        }
        # 폼은 다음 글을 위해 기본값으로 돌아간다
        assert _wait(
            lambda: (
                page.evaluate(
                    "() => document.querySelector('#new-knobs [data-knob=thinking]').value"
                )
                == ""
            )
        )
        ctx.close()

    def test_bad_value_shows_the_reason_and_keeps_the_title(self, board, browser):
        ctx, page = _page(browser, board)
        page.wait_for_selector("#new-adv:not([hidden])", timeout=8000)
        page.click("#new-adv")
        page.fill("#new-topic", "잘못된 값")
        page.fill("#new-knobs [data-knob=max_agents]", "many")
        page.click("#new-create")
        assert _wait(lambda: "Bad knob value" in page.inner_text("body"))
        assert board.store.list_posts() == []
        assert page.input_value("#new-topic") == "잘못된 값"
        ctx.close()

    def test_old_agent_cli_hides_the_advanced_form(self, board, browser, monkeypatch):
        from agent_board import knobs_spec

        monkeypatch.setattr(knobs_spec, "load", lambda b: tuple(knobs_spec._MODEL_ONLY))
        ctx, page = _page(browser, board)
        page.wait_for_function(
            "() => document.querySelector('#new-model').options.length > 0",
            timeout=8000,
        )
        page.wait_for_timeout(300)
        assert page.locator("#new-adv").is_hidden()
        page.fill("#new-topic", "구버전")
        page.click("#new-create")
        assert _wait(lambda: len(board.store.list_posts()) == 1)
        assert board.store.list_posts()[0].knobs == {"model": "m"}
        ctx.close()


class TestCardIsAMirror:
    def test_model_chip_and_changed_knob_chips(self, board, browser):
        board.seed_post(
            topic="t",
            knobs={"model": "m", "thinking": "on", "auditor": True, "max_agents": 10},
        )
        ctx, page = _page(browser, board)
        page.wait_for_selector(".model-chip", timeout=8000)
        assert page.inner_text(".model-chip").strip() == "🤖 m"
        chips = page.evaluate(
            "() => Array.from(document.querySelectorAll('.knob-chip')).map(e => e.textContent.trim())"
        )
        # max_agents=10 은 기본값이라 칩 없음; 칩 아이콘은 표의 ui.icon(🧠, 🔎)
        assert chips == ["🧠 on", "🔎 on"]
        assert page.locator(".model-sel").count() == 0  # 드롭다운은 없다
        ctx.close()

    def test_panel_is_read_only_and_lists_every_knob(self, board, browser):
        board.seed_post(topic="t", knobs={"model": "m", "stall": "10m"})
        ctx, page = _page(browser, board)
        page.wait_for_selector(".post .knobs", timeout=8000)
        assert page.locator(".knob-panel").is_hidden()
        page.click(".post .knobs")
        assert page.locator(".knob-panel").is_visible()
        txt = page.inner_text(".knob-panel")
        for word in (
            "Model",
            "Thinking",
            "Agent cap",
            "Stream stall limit",
            "Session audit",
            "(default)",
            "10m",
        ):
            assert word in txt, word
        assert "chips inside" in txt
        assert (
            page.locator(
                ".knob-panel input, .knob-panel select, .knob-panel button"
            ).count()
            == 0
        )
        ctx.close()

    def test_live_values_from_the_room_win_and_are_stored(self, board, browser):
        """방이 보고한 값(status.json knobs)이 카드에 보이고 저장된다 — 방에서
        🤖 로 바꾼 모델이 다음 spawn 에 남는다."""
        pid = board.seed_post(
            topic="t",
            knobs={"model": "m", "thinking": "off"},
            live_knobs={"model": "Qwen-live", "thinking": "on", "auditor": False},
        )
        ctx, page = _page(browser, board)
        page.wait_for_selector(".model-chip", timeout=8000)
        assert page.inner_text(".model-chip").strip() == "🤖 Qwen-live"
        assert _wait(lambda: board.store.get(pid).knobs["model"] == "Qwen-live")
        ctx.close()

    def test_panel_stays_open_across_live_updates(self, board, browser):
        import json

        pid = board.seed_post(
            topic="t", knobs={"model": "m"}, live_knobs={"model": "m"}
        )
        ctx, page = _page(browser, board)
        page.wait_for_selector(".post .knobs", timeout=8000)
        page.click(".post .knobs")
        assert page.locator(".knob-panel").is_visible()
        # 방이 값을 바꾼다 → SSE 로 카드가 다시 그려져도 패널은 펼쳐진 채
        post = board.store.get(pid)
        sdir = (
            board.cfg.workspace_for(pid) / ".agent-cli" / "sessions" / post.session_id
        )
        time.sleep(1.1)  # mtime 해상도
        (sdir / "status.json").write_text(
            json.dumps(
                {
                    "busy": False,
                    "awaiting_input": False,
                    "viewers": 0,
                    "knobs": {"model": "m", "thinking": "on"},
                }
            )
        )
        assert _wait(
            lambda: (
                "⚙ on" in (page.inner_text(".knob-panel") or "")
                or "on" in (page.inner_text(".knob-panel") or "")
            ),
            timeout=10,
        )
        assert page.locator(".knob-panel").is_visible()
        ctx.close()


class TestCloneForm:
    def test_clone_form_is_prefilled_from_the_source(self, board, browser):
        board.seed_post(
            topic="src", knobs={"model": "m", "thinking": "on", "max_agents": 3}
        )
        ctx, page = _page(browser, board)
        page.wait_for_selector(".post .clone", timeout=8000)
        page.click(".post .clone")
        page.wait_for_selector("#clone-dlg[open]", timeout=8000)
        page.click("#clone-adv")
        assert page.locator("#clone-knobs").is_visible()
        assert (
            page.evaluate(
                "() => document.querySelector('#clone-knobs [data-knob=thinking]').value"
            )
            == "on"
        )
        assert page.input_value("#clone-knobs [data-knob=max_agents]") == "3"
        page.select_option("#clone-knobs [data-knob=thinking]", "off")
        page.click("#clone-go")
        assert _wait(lambda: len(board.store.list_posts()) == 2)
        clone = next(p for p in board.store.list_posts() if p.topic.endswith("(clone)"))
        assert clone.knobs == {"model": "m", "thinking": "off", "max_agents": 3}
        ctx.close()
