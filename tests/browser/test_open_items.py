"""📋 열린 항목 칩 (v1.44.0) — 실브라우저 계약.

방이 status.json 에 쓴 ``open_items`` 요약을 카드가 ``📋 N`` 칩으로 그린다.
title 은 id 를 열린 순서로 ` · ` 로 잇고, 되묻기 예산이 0 이면 ``oi-warn``.
필드가 없거나 count 0 이면 칩이 없다. status.json 이 바뀌면 SSE 재렌더로
칩이 따라 바뀐다(``awaiting_input`` 배지와 같은 길).
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


class TestOpenItemsChip:
    def test_chip_count_title_and_warn_follow_status(self, board, browser):
        pid = board.seed_post(
            topic="ledger",
            open_items={
                "count": 3,
                "ids": ["u-2", "n-1", "r-1"],
                "oldest_turns": 7,
                "budget_left": 6,
            },
        )
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector(".badge.oi", timeout=8000)
        chip = page.locator(".badge.oi")
        assert chip.inner_text().strip() == "📋 3"
        assert chip.get_attribute("title") == "u-2 · n-1 · r-1"
        assert page.locator(".badge.oi-warn").count() == 0
        # 모델 칩과 같은 높이
        h_oi = chip.bounding_box()["height"]
        h_model = page.locator(".model-chip").bounding_box()["height"]
        assert abs(h_oi - h_model) <= 1

        # 예산 소진 → SSE 재렌더로 경고색 (mtime 이 확실히 바뀌게 앞당긴다)
        sp = _status_path(board, pid)
        status = json.loads(sp.read_text())
        status["open_items"] = {
            "count": 3,
            "ids": ["u-2", "n-1", "r-1"],
            "oldest_turns": 9,
            "budget_left": 0,
        }
        sp.write_text(json.dumps(status))
        t = time.time() + 5
        os.utime(sp, (t, t))
        assert _wait(lambda: page.locator(".badge.oi.oi-warn").count() == 1)

        # 0 개 → 칩 사라짐
        status["open_items"] = {
            "count": 0,
            "ids": [],
            "oldest_turns": None,
            "budget_left": 0,
        }
        sp.write_text(json.dumps(status))
        t += 5
        os.utime(sp, (t, t))
        assert _wait(lambda: page.locator(".badge.oi").count() == 0)
        ctx.close()

    def test_no_chip_without_field(self, board, browser):
        board.seed_post(topic="old-room")  # open_items 없음(구버전 방)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector(".post", timeout=8000)
        page.wait_for_timeout(500)
        assert page.locator(".badge.oi").count() == 0
        ctx.close()
