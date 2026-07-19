"""대화방 복제 모달 실브라우저 e2e (v1.21.0).

각 글 카드의 '복제' 버튼 → 모달(주제·모델·트리) → [복제 생성]. 닫기/취소
= 중단. 트리 lazy 로드·체크박스 dedupe·모달 생명주기는 실 브라우저에서만
end-to-end 검증된다.
"""

from __future__ import annotations

import json
import time


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


def _open_clone_modal(board, browser, source_id):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(board.url, wait_until="load")
    # 원본 글 카드의 복제 버튼 (그 카드로 스코프)
    page.wait_for_selector(f'.post[data-id="{source_id}"] .clone', timeout=8000)
    page.click(f'.post[data-id="{source_id}"] .clone')
    page.wait_for_selector("#clone-dlg[open]", timeout=4000)
    return ctx, page


class TestCloneModal:
    def test_clone_files_via_modal(self, board, browser):
        src = board.seed_post(topic="원본 프로젝트")
        ws = board.cfg.workspace_for(src)
        (ws / "main.py").write_text("print('orig')")
        (ws / "notes.md").write_text("# notes")

        ctx, page = _open_clone_modal(board, browser, src)
        # 원본이 미리 스코프됨 (라벨)
        assert "원본 프로젝트" in page.inner_text("#clone-src-label")
        page.fill("#clone-topic", "복제본")
        page.wait_for_selector("#clone-tree .clone-node", timeout=6000)
        page.locator(
            "#clone-tree .clone-node:has-text('main.py') input[type=checkbox]"
        ).check()
        page.click("#clone-go")

        assert _wait(lambda: _has_post(board, "복제본"))
        new_ws = board.cfg.workspace_for(_post_id(board, "복제본"))
        assert _wait(lambda: (new_ws / "main.py").exists())
        assert (new_ws / "main.py").read_text() == "print('orig')"
        assert not (new_ws / "notes.md").exists()
        assert _wait(lambda: not page.locator("#clone-dlg[open]").count())
        ctx.close()

    def test_clone_conversation_via_agent_cli(self, board, browser):
        src = board.seed_post(topic="대화 원본")
        ws = board.cfg.workspace_for(src)
        sdir = ws / ".agent-cli" / "sessions" / "1111111111"
        sdir.mkdir(parents=True)
        sdir.joinpath("session.jsonl").write_text(
            json.dumps({"_meta": {"session_id": "1111111111", "workspace": str(ws)}})
            + "\n"
        )
        sdir.joinpath("history.jsonl").write_text('{"role":"user"}\n')

        ctx, page = _open_clone_modal(board, browser, src)
        page.fill("#clone-topic", "이어받은 방")
        page.wait_for_selector(
            "#clone-tree .clone-node:has-text('.agent-cli')", timeout=6000
        )
        page.locator(
            "#clone-tree .clone-node:has-text('.agent-cli') input[type=checkbox]"
        ).first.check()
        page.click("#clone-go")

        assert _wait(lambda: _has_post(board, "이어받은 방"))
        new_id = _post_id(board, "이어받은 방")
        assert _wait(lambda: board.store.get(new_id).session_id is not None)
        sid = board.store.get(new_id).session_id
        newdir = board.cfg.workspace_for(new_id) / ".agent-cli" / "sessions" / sid
        assert newdir.is_dir()
        assert (newdir / "history.jsonl").exists()
        ctx.close()

    def test_cancel_aborts_no_post_created(self, board, browser):
        src = board.seed_post(topic="원본")
        (board.cfg.workspace_for(src) / "f.py").write_text("x")
        ctx, page = _open_clone_modal(board, browser, src)
        before = len(board.store.list_posts())
        page.fill("#clone-topic", "취소될 방")
        page.click("#clone-cancel")
        assert _wait(lambda: not page.locator("#clone-dlg[open]").count())
        page.wait_for_timeout(400)
        assert len(board.store.list_posts()) == before
        assert not _has_post(board, "취소될 방")
        ctx.close()

    def test_backdrop_click_closes(self, board, browser):
        src = board.seed_post(topic="원본")
        (board.cfg.workspace_for(src) / "f.py").write_text("x")
        ctx, page = _open_clone_modal(board, browser, src)
        box = page.evaluate(
            "() => { const d = document.getElementById('clone-dlg');"
            " const r = d.getBoundingClientRect();"
            " return {x: r.left, y: r.top}; }"
        )
        page.mouse.click(max(0, box["x"] - 20), max(0, box["y"] - 20))
        assert _wait(lambda: not page.locator("#clone-dlg[open]").count())
        ctx.close()

    def test_empty_source_message(self, board, browser):
        empty = board.store.create_post(topic="빈방")
        board.cfg.workspace_for(empty.post_id).mkdir(parents=True, exist_ok=True)
        ctx, page = _open_clone_modal(board, browser, empty.post_id)
        assert _wait(lambda: "복사할 파일이 없" in page.inner_text("#clone-tree"))
        ctx.close()


def _has_post(board, topic):
    return any(p.topic == topic for p in board.store.list_posts())


def _post_id(board, topic):
    return next(p.post_id for p in board.store.list_posts() if p.topic == topic)
