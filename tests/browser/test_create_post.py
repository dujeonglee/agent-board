"""글 생성의 **중복 제출** 계약 (사용자 제보: 같은 제목으로 2개).

두 경로가 겹쳐 있었다:

1. **IME** — 한글 조합을 Enter 로 확정하면 `keydown` 이 한 번
   (`isComposing=true`), 실제 제출로 또 한 번. 둘 다 같은 제목을 읽는다.
   그래서 **한글로 칠 때만** 재현되는 버그였다.
2. **가드 부재** — 입력을 `await` 뒤에 비워서 그 사이 두 번째 호출이 같은
   값을 통과시킨다. 더블클릭·Enter+클릭도 같다.

둘 다 실브라우저 없이는 못 잡는다 — IME 는 `isComposing` 이라는 **실제 키
이벤트 속성**이고, 경합은 fetch 가 떠 있는 동안의 타이밍이다.
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


def _titles(board):
    return [p.topic for p in board.store.list_posts()]


class TestCreatePostDoubleSubmit:
    def test_ime_confirm_enter_does_not_create_a_post(self, board, browser):
        """조합 확정 Enter(`isComposing=true`)는 제출이 아니다."""
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector("#new-topic", timeout=8000)
        page.fill("#new-topic", "한글 제목")

        # 조합 확정 Enter — 브라우저가 IME 중에 내는 그 이벤트.
        page.evaluate(
            """() => document.getElementById('new-topic').dispatchEvent(
                 new KeyboardEvent('keydown',
                   {key: 'Enter', isComposing: true, bubbles: true}))"""
        )
        time.sleep(0.4)
        assert _titles(board) == [], "조합 확정 Enter 로 글이 생겼다"

        # 진짜 제출 Enter — 이제 하나 생긴다.
        page.press("#new-topic", "Enter")
        assert _wait(lambda: _titles(board) == ["한글 제목"]), _titles(board)
        ctx.close()

    def test_two_enters_in_flight_create_one_post(self, board, browser):
        """진행 중 가드. 종전엔 입력을 `await` 뒤에 비워, 두 번째 Enter 가
        같은 제목을 그대로 읽어 글이 둘 생겼다."""
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector("#new-topic", timeout=8000)
        page.fill("#new-topic", "중복 금지")

        # 같은 tick 에 두 번 — 첫 fetch 가 끝나기 전에 두 번째가 들어온다.
        page.evaluate(
            """() => {
                 const el = document.getElementById('new-topic');
                 const ev = () => new KeyboardEvent('keydown',
                   {key: 'Enter', bubbles: true});
                 el.dispatchEvent(ev());
                 el.dispatchEvent(ev());
               }"""
        )
        assert _wait(lambda: _titles(board) == ["중복 금지"]), _titles(board)
        time.sleep(0.6)  # 늦게 도착하는 두 번째가 있으면 여기서 드러난다
        assert _titles(board) == ["중복 금지"], _titles(board)
        # 성공하면 입력을 비운다 — 안 비우면 다음 글이 같은 제목으로 나간다.
        assert page.input_value("#new-topic") == ""
        ctx.close()

    def test_enter_then_click_creates_one_post(self, board, browser):
        """가드는 `creating` 플래그 **하나**다 — Enter 든 클릭이든 같은
        `create()` 로 들어오므로 경로마다 장치가 따로 필요 없다. 버튼
        비활성화는 가드가 아니라 **피드백**이다(아래 테스트가 따로 고정)."""
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector("#new-topic", timeout=8000)
        page.fill("#new-topic", "엔터와 클릭")
        page.evaluate(
            """() => {
                 document.getElementById('new-topic').dispatchEvent(
                   new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
                 document.getElementById('new-create').click();
               }"""
        )
        assert _wait(lambda: _titles(board) == ["엔터와 클릭"]), _titles(board)
        time.sleep(0.6)
        assert _titles(board) == ["엔터와 클릭"], _titles(board)
        ctx.close()

    def test_button_is_disabled_while_the_request_is_in_flight(self, board, browser):
        """피드백 계약 — 누른 게 먹었는지 모르면 사용자가 또 누른다.
        (중복 자체는 `creating` 플래그가 막지만, 그건 화면에 안 보인다.)"""
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector("#new-topic", timeout=8000)

        # 요청을 붙잡아 in-flight 상태를 관찰 가능하게 만든다.
        page.route("**/api/posts", lambda route: None)
        page.fill("#new-topic", "느린 요청")
        page.press("#new-topic", "Enter")
        assert _wait(lambda: page.is_disabled("#new-create"))
        ctx.close()

    def test_failed_create_gives_the_title_back(self, board, browser):
        """실패하면 사용자가 친 제목을 돌려준다 — 날리면 다시 쳐야 한다."""
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(board.url, wait_until="load")
        page.wait_for_selector("#new-topic", timeout=8000)
        page.route("**/api/posts", lambda route: route.fulfill(status=500))
        page.fill("#new-topic", "실패할 제목")
        page.press("#new-topic", "Enter")
        assert _wait(lambda: page.input_value("#new-topic") == "실패할 제목"), (
            page.input_value("#new-topic")
        )
        assert _titles(board) == []
        ctx.close()
