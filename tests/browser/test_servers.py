"""모델 서버 여러 개 (v1.48.0) — 실브라우저 계약.

- admin: 서버가 없으면 셋업 화면("Add your first server"), 서버 둘 추가, Make default, 방이 있는
  서버 삭제는 방 수와 함께 확인, 기본 서버는 Delete 비활성, 옛 모양 파일은 .v1 안내.
- 새 글 폼: 서버를 바꾸면 모델 선택지가 그 서버의 것으로 바뀌고 그 서버의 기본 모델이 골라진다.
  서버가 없으면 admin 으로 가는 안내.
- 카드의 🖥 서버 칩, 복제 대화상자가 원본의 서버를 미리 고른다.

모델 목록 프로브는 닫힌 포트(127.0.0.1:9)로 가 실패한다 — 표는 등록된 것만 보인다.
"""

from __future__ import annotations

import json
import time

import pytest


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


def _cfg(board):
    return json.loads(board.cfg.agent_cli_config_json.read_text())


def _empty(board):
    board.cfg.agent_cli_config_json.unlink()
    board.cfg.models_json.unlink()


def _add(page, name, url, default_model=""):
    page.click("#srv-add")
    page.fill("#cfg-name", name)
    page.fill("#cfg-base-url", url)
    if default_model:
        page.fill("#cfg-default-model", default_model)
    page.click("#cfg-save")
    page.wait_for_function(
        "(n) => document.querySelector('#srv-title').textContent === n", arg=name
    )


@pytest.fixture(autouse=True)
def _need_agent_cli():
    pytest.importorskip("agent_cli.config")


def test_setup_screen_then_two_servers_default_and_delete(board, browser):
    _empty(board)
    board.cfg.agent_cli_config_json.write_text(json.dumps({"provider": "openai"}))
    page = browser.new_page()
    try:
        page.goto(board.url + "admin")
        page.wait_for_function(
            "document.querySelector('#srv-title').textContent === 'Add your first server'"
        )
        assert page.locator("#srv-default-badge").is_hidden()  # 셋업 화면엔 배지 없음
        intro = page.locator("#srv-intro").inner_text()
        assert "config.json.v1" in intro  # 옛 모양 파일 안내
        assert page.locator("#models").is_hidden()

        page.fill("#cfg-name", "home")
        page.fill("#cfg-base-url", "http://127.0.0.1:9/v1")
        page.click("#cfg-save")
        page.wait_for_function(
            "document.querySelector('#srv-title').textContent === 'home'"
        )
        assert "Old files kept as" in page.locator("#cfg-status").inner_text()
        assert "Pick the default model" in page.locator("#cfg-status").inner_text()
        assert (
            board.cfg.agent_cli_config_json.parent / "agent-cli-config.json.v1"
        ).exists()
        assert _cfg(board)["endpoint"] == "home"
        assert page.locator("#models").is_visible()
        assert page.locator("#srv-delete").is_disabled()  # 기본 서버는 못 지운다
        # 모델을 저장하면 왼쪽 목록의 모델 수도 따라온다(폼은 그대로)
        page.fill("#cfg-default-model", "unsaved-input")
        page.evaluate(
            """() => fetch('/api/admin/endpoints/home/models/m', {method: 'PUT',
                 headers: {'Content-Type': 'application/json'},
                 body: JSON.stringify({dialect: 'json_fc'})})"""
        )
        page.click("#models-refresh")  # 표에 m 이 보이게 다시 읽는다
        page.locator("#models-body tr", has_text="m").locator(
            "button", has_text="✎"
        ).click()
        page.click("#entry-save")
        page.wait_for_function(
            "document.querySelector('#srv-list .srv-meta').textContent.includes('1 model')"
        )
        assert page.locator("#cfg-default-model").input_value() == "unsaved-input"

        _add(page, "work", "https://gw.example.com/v1")
        assert _cfg(board)["endpoint"] == "home"  # 둘째는 기본이 아니다
        assert page.locator("#srv-list button").count() == 2
        page.click("#srv-default")
        page.wait_for_function("!document.querySelector('#srv-default-badge').hidden")
        assert _cfg(board)["endpoint"] == "work"

        # home 에 방 하나 → 삭제는 방 수를 보여 주고 확인
        board.store.create_post(topic="r", knobs={"model": "m"}, endpoint="home")
        page.reload()
        page.locator("#srv-list button", has_text="home").click()
        page.wait_for_function(
            "document.querySelector('#srv-title').textContent === 'home'"
        )
        seen = []

        def on_dialog(d):
            seen.append(d.message)
            d.accept()

        page.on("dialog", on_dialog)
        page.click("#srv-delete")
        assert _wait(lambda: "home" not in _cfg(board)["endpoints"])
        assert "1 room(s) use this server" in seen[0]
        assert "clone them to another server" in seen[0]
    finally:
        page.close()


def test_new_post_form_follows_the_server(board, browser):
    board.cfg.agent_cli_config_json.write_text(
        json.dumps(
            {
                "endpoint": "home",
                "endpoints": {
                    "home": {"base_url": "http://127.0.0.1:9/v1", "default_model": "m"},
                    "work": {"base_url": "http://127.0.0.1:9/w", "default_model": "w2"},
                },
            }
        )
    )
    board.cfg.models_json.write_text(
        json.dumps(
            {
                "endpoints": {
                    "home": {"models": {"m": {"dialect": "json_fc"}}},
                    "work": {
                        "models": {
                            "w1": {"dialect": "json_fc"},
                            "w2": {"dialect": "xml_fc"},
                            "nope": {},
                        }
                    },
                }
            }
        )
    )
    page = browser.new_page()
    try:
        page.goto(board.url)
        page.wait_for_function("document.querySelector('#new-model').value === 'm'")
        assert page.locator("#new-endpoint").input_value() == "home"
        page.select_option("#new-endpoint", "work")
        page.wait_for_function("document.querySelector('#new-model').value === 'w2'")
        opts = page.evaluate(
            "() => [...document.querySelectorAll('#new-model option')].map(o => o.value)"
        )
        assert opts == ["", "w1", "w2"]  # 바인딩 없는 'nope' 는 없다
        page.fill("#new-topic", "on work")
        page.click("#new-create")
        assert _wait(lambda: [p.endpoint for p in board.store.list_posts()] == ["work"])
        page.wait_for_selector(".server-chip")
        assert "🖥 work" in page.locator(".server-chip").first.inner_text()

        # 복제는 원본의 서버를 미리 고른다
        page.locator(".post .clone").first.click()
        page.wait_for_function(
            "document.querySelector('#clone-endpoint').value === 'work'"
        )
        assert page.locator("#clone-model").input_value() == "w2"
    finally:
        page.close()


def test_no_server_points_to_admin(board, browser):
    _empty(board)
    page = browser.new_page()
    try:
        page.goto(board.url)
        page.wait_for_selector("#new-no-server", state="visible")
        assert page.locator("#new-no-server a").get_attribute("href") == "/admin"
        assert page.locator("#new-create").is_disabled()
    finally:
        page.close()
