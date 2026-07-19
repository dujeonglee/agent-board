"""대화방 clone 실브라우저 e2e — 원본 선택 → 트리 체크 → 생성 → 복사.

트리 lazy 로드·체크박스 수집·조상 dedupe 는 프런트 JS 로직이라 실
브라우저에서만 end-to-end 검증된다.
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


class TestCloneFlow:
    def test_clone_files_via_tree_picker(self, board, browser):
        # 원본 글 + 워크스페이스 파일 시드
        src = board.seed_post(topic="원본 프로젝트")
        ws = board.cfg.workspace_for(src)
        (ws / "main.py").write_text("print('orig')")
        (ws / "notes.md").write_text("# notes")

        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector("#clone-toggle", timeout=8000)

        page.fill("#new-topic", "복제본")
        page.click("#clone-toggle")
        page.wait_for_selector("#clone-panel:not([hidden])", timeout=4000)
        # 원본 선택 → 트리 렌더
        page.select_option("#clone-source", value=src)
        page.wait_for_selector("#clone-tree .clone-node", timeout=6000)
        # main.py 체크박스만 체크
        page.locator(".clone-node:has-text('main.py') input[type=checkbox]").check()
        page.click("#new-create")

        # 새 글 생성 후 clone executor 완료(복사)를 파일 존재로 기다린다 —
        # DB row 는 복사보다 먼저 커밋되므로 _has_post 만으론 이르다.
        assert _wait(lambda: _has_post(board, "복제본"))
        new_ws = board.cfg.workspace_for(_post_id(board, "복제본"))
        assert _wait(lambda: (new_ws / "main.py").exists())
        assert (new_ws / "main.py").read_text() == "print('orig')"
        assert not (new_ws / "notes.md").exists()  # 체크 안 함
        ctx.close()

    def test_clone_conversation_via_agent_cli_dir(self, board, browser):
        import json

        src = board.seed_post(topic="대화 원본")
        ws = board.cfg.workspace_for(src)
        sdir = ws / ".agent-cli" / "sessions" / "1111111111"
        sdir.mkdir(parents=True)
        sdir.joinpath("session.jsonl").write_text(
            json.dumps({"_meta": {"session_id": "1111111111", "workspace": str(ws)}})
            + "\n"
        )
        sdir.joinpath("history.jsonl").write_text('{"role":"user"}\n')

        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.fill("#new-topic", "이어받은 방")
        page.click("#clone-toggle")
        page.select_option("#clone-source", value=src)
        page.wait_for_selector(".clone-node:has-text('.agent-cli')", timeout=6000)
        page.locator(
            ".clone-node:has-text('.agent-cli') input[type=checkbox]"
        ).first.check()
        page.click("#new-create")

        assert _wait(lambda: _has_post(board, "이어받은 방"))
        new_id = _post_id(board, "이어받은 방")
        # 세션 remap → DB session_id 세팅 (첫 open 시 --resume)
        assert _wait(lambda: board.store.get(new_id).session_id is not None)
        sid = board.store.get(new_id).session_id
        newdir = board.cfg.workspace_for(new_id) / ".agent-cli" / "sessions" / sid
        assert newdir.is_dir()
        assert (newdir / "history.jsonl").exists()
        ctx.close()


def _has_post(board, topic):
    return any(p.topic == topic for p in board.store.list_posts())


def _post_id(board, topic):
    return next(p.post_id for p in board.store.list_posts() if p.topic == topic)
