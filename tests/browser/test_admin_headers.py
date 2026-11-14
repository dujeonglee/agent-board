"""admin 의 Extra headers 표 (v1.47.0) — 실브라우저 계약.

추가 → 저장 → 다시 열기에서 값은 ``***`` 로만 보이고, 체크를 끄고 ``***`` 를 그대로 둔 채
저장하면 저장된 값은 그대로·꺼짐으로 남는다. Remove 한 행은 파일에서 사라진다.
"""

from __future__ import annotations

import json

import pytest

_V = "test-hdr-browser-value"


def _saved(board):
    return json.loads(board.cfg.agent_cli_config_json.read_text())


def test_add_disable_save_reopen(board, browser):
    pytest.importorskip("agent_cli.providers.credentials")
    board.cfg.agent_cli_config_json.write_text(
        json.dumps({"provider": "openai", "base_url": "http://127.0.0.1:9/v1"})
    )
    page = browser.new_page()
    try:
        page.goto(board.url + "admin")
        page.wait_for_selector("#cfg-hdr-add")
        assert page.locator("#cfg-hdrs-body tr").count() == 0

        # 추가 두 행 → 저장
        page.click("#cfg-hdr-add")
        page.locator("#cfg-hdrs-body tr").nth(0).locator(".hdr-name").fill(
            "x-service-id"
        )
        page.locator("#cfg-hdrs-body tr").nth(0).locator(".hdr-value").fill(_V)
        page.click("#cfg-hdr-add")
        page.locator("#cfg-hdrs-body tr").nth(1).locator(".hdr-name").fill("x-drop")
        page.locator("#cfg-hdrs-body tr").nth(1).locator(".hdr-value").fill("v2")
        assert (
            page.locator("#cfg-hdrs-body tr")
            .nth(0)
            .locator(".hdr-value")
            .get_attribute("type")
            == "password"
        )
        page.click("#cfg-save")
        page.wait_for_selector("#cfg-status.ok")
        assert _saved(board)["extra_headers"] == [
            {"name": "x-service-id", "value": _V, "enabled": True},
            {"name": "x-drop", "value": "v2", "enabled": True},
        ]

        # 다시 열기 — 값은 *** 로만
        page.reload()
        page.wait_for_function(
            "document.querySelectorAll('#cfg-hdrs-body tr').length === 2"
        )
        row0 = page.locator("#cfg-hdrs-body tr").nth(0)
        assert row0.locator(".hdr-name").input_value() == "x-service-id"
        assert row0.locator(".hdr-value").input_value() == "***"
        assert _V not in page.content()

        # 끄기 + 둘째 행 Remove → 저장: 값은 유지, 꺼짐, x-drop 은 사라짐
        row0.locator(".hdr-on").uncheck()
        page.locator("#cfg-hdrs-body tr").nth(1).locator(".hdr-remove").click()
        page.click("#cfg-save")
        page.wait_for_function(
            "document.querySelector('#cfg-status').textContent.startsWith('Saved')"
        )
        assert _saved(board)["extra_headers"] == [
            {"name": "x-service-id", "value": _V, "enabled": False}
        ]
        page.reload()
        page.wait_for_function(
            "document.querySelectorAll('#cfg-hdrs-body tr').length === 1"
        )
        row0 = page.locator("#cfg-hdrs-body tr").nth(0)
        assert row0.locator(".hdr-on").is_checked() is False
        assert row0.locator(".hdr-value").input_value() == "***"

        # 금지 이름은 저장 실패 문구 — 파일은 그대로
        page.click("#cfg-hdr-add")
        page.locator("#cfg-hdrs-body tr").nth(1).locator(".hdr-name").fill(
            "Authorization"
        )
        page.locator("#cfg-hdrs-body tr").nth(1).locator(".hdr-value").fill("x")
        page.click("#cfg-save")
        page.wait_for_selector("#cfg-status.err")
        assert "cannot be overridden" in page.locator("#cfg-status").inner_text()
        assert len(_saved(board)["extra_headers"]) == 1
    finally:
        page.close()
