"""🧵 작업 칩 (v1.46.0) — 실브라우저 계약.

방이 status.json 에 쓴 ``jobs`` 요약(돌고 있는 백그라운드 작업, agent-cli ≥ 10.121.0)을
카드가 ``🧵 N`` 칩으로 그린다. 📋 칩 뒤. title 에 작업마다 명령과 시작 시각. 필드를 지우면
칩이 사라진다. status.json 이 바뀌면 SSE 재렌더로 따라 바뀐다. 멈춤 버튼은 없다 — 거울.
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


def _job(i, cmd):
    return {
        "id": i,
        "pid": 4000 + i,
        "owner": "main",
        "command": cmd,
        "started_at": time.time() - 600,
    }


class TestJobsChip:
    def test_chip_title_order_and_removal_follow_status(self, board, browser):
        pid = board.seed_post(
            topic="serving",
            jobs={"count": 1, "list": [_job(1, "python3 -m http.server 8080")]},
            open_items={
                "count": 1,
                "ids": ["u-1"],
                "oldest_turns": 1,
                "budget_left": 8,
            },
        )
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector(".badge.jobs", timeout=8000)
        chip = page.locator(".badge.jobs")
        assert chip.inner_text().strip() == "🧵 1"
        title = chip.get_attribute("title")
        assert title.startswith("Background jobs running — stop them from the 🧵 chips")
        assert "#1 python3 -m http.server 8080 — started " in title
        side = page.locator(".post-side .st").first
        order = side.evaluate(
            "el => [...el.querySelectorAll('.badge')].map(b => b.className)"
        )
        assert order.index("badge oi") < order.index("badge jobs")
        h_jobs = chip.bounding_box()["height"]
        h_oi = page.locator(".badge.oi").bounding_box()["height"]
        assert abs(h_jobs - h_oi) <= 1
        assert page.locator(".post-side button:has-text('■')").count() == 0

        # 작업이 하나 더 → SSE 재렌더
        sp = _status_path(board, pid)
        status = json.loads(sp.read_text())
        status["jobs"] = {
            "count": 2,
            "list": [_job(1, "python3 -m http.server 8080"), _job(2, "make test")],
        }
        sp.write_text(json.dumps(status))
        t = time.time() + 5
        os.utime(sp, (t, t))
        assert _wait(lambda: page.locator(".badge.jobs").inner_text().strip() == "🧵 2")
        assert "#2 make test" in page.locator(".badge.jobs").get_attribute("title")

        # 모두 끝남 → 필드 생략 → 칩 사라짐
        del status["jobs"]
        sp.write_text(json.dumps(status))
        t += 5
        os.utime(sp, (t, t))
        assert _wait(lambda: page.locator(".badge.jobs").count() == 0)
        assert page.locator(".badge.oi").count() == 1
        ctx.close()
