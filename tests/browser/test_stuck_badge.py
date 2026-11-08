"""🔁 제자리 배지 (v1.45.0) — 실브라우저 계약.

방이 status.json 에 쓴 ``stuck`` 요약(main 런이 새 결과 없이 N 턴째)을 카드가
``🔁 N turns`` 배지로 그린다. ``asking`` 이면 방이 Continue/Stop 을 묻는 중이라
``stuck-ask``. 필드를 지우면 배지가 사라진다. status.json 이 바뀌면 SSE 재렌더로
따라 바뀐다(📋 칩·``awaiting_input`` 배지와 같은 길).
"""

from __future__ import annotations

import json
import os
import time


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


def _status_path(board, post_id):
    post = board.store.get(post_id)
    return (
        board.cfg.workspace_for(post_id)
        / ".agent-cli"
        / "sessions"
        / post.session_id
        / "status.json"
    )


class TestStuckBadge:
    def test_badge_title_ask_and_removal_follow_status(self, board, browser):
        pid = board.seed_post(
            topic="spinning",
            stuck={"turns": 4, "since_turn": 31, "asking": False},
            open_items={
                "count": 1,
                "ids": ["u-1"],
                "oldest_turns": 4,
                "budget_left": 6,
            },
        )
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector(".badge.stuck", timeout=8000)
        badge = page.locator(".badge.stuck")
        assert badge.inner_text().strip() == "🔁 4 turns"
        assert badge.get_attribute("title") == "No new results for 4 turns"
        assert page.locator(".badge.stuck-ask").count() == 0
        # 상태 라벨 다음·📋 칩 앞, 📋 칩과 같은 높이
        side = page.locator(".post-side .st").first
        order = side.evaluate(
            "el => [...el.querySelectorAll('.badge')].map(b => b.className)"
        )
        assert order.index("badge stuck") < order.index("badge oi")
        h_stuck = badge.bounding_box()["height"]
        h_oi = page.locator(".badge.oi").bounding_box()["height"]
        assert abs(h_stuck - h_oi) <= 1

        # 방이 Continue/Stop 을 묻는다 → SSE 재렌더로 stuck-ask (mtime 을 앞당긴다)
        sp = _status_path(board, pid)
        status = json.loads(sp.read_text())
        status["stuck"] = {"turns": 6, "since_turn": 31, "asking": True}
        status["awaiting_input"] = True
        sp.write_text(json.dumps(status))
        t = time.time() + 5
        os.utime(sp, (t, t))
        assert _wait(lambda: page.locator(".badge.stuck.stuck-ask").count() == 1)
        assert page.locator(".badge.stuck").inner_text().strip() == "🔁 6 turns"
        assert page.locator(".badge.stuck").get_attribute("title") == (
            "No new results for 6 turns — waiting for your answer in the room"
        )

        # 필드를 지움 → 배지 사라짐
        del status["stuck"]
        status["awaiting_input"] = False
        sp.write_text(json.dumps(status))
        t += 5
        os.utime(sp, (t, t))
        assert _wait(lambda: page.locator(".badge.stuck").count() == 0)
        assert page.locator(".badge.oi").count() == 1
        ctx.close()
