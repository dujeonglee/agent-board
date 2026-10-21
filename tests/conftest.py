"""테스트 전역 설정."""

from __future__ import annotations

import os

# 실브라우저 스위트(tests/browser)는 옵트인 아니면 수집조차 안 한다 —
# per-item skip 만으로는 pytest-asyncio 가 수집 단계 이벤트루프를 남겨
# 이후 async 테스트를 깨뜨린다(agent-cli 에서 실측). 로컬/수동 실행 전용
# (이 저장소는 아직 CI 없음): AGENT_BOARD_BROWSER_TESTS=1 pytest tests/browser/
if os.environ.get("AGENT_BOARD_BROWSER_TESTS") != "1":
    collect_ignore = ["browser"]

import pytest

# agent-cli 의 노브 표 (`agent-cli knobs --json`, v1.36.0) 의 고정본 — 테스트는
# 바이너리를 부르지 않는다(속도·결정성). 실 바이너리와의 계약은
# tests/test_knobs_spec.py::TestRealBinary 하나가 잰다(설치돼 있을 때만).
FAKE_SPEC = (
    {
        "name": "model",
        "label": "Model",
        "help": "",
        "flag": "--model",
        "short": "-m",
        "env": "AGENT_CLI_MODEL",
        "kind": "text",
        "choices": None,
        "default": None,
        "ui": {"icon": "🤖", "unit": "", "choice_labels": {}},
    },
    {
        "name": "thinking",
        "label": "Thinking",
        "help": "Model thinking",
        "flag": "--thinking",
        "short": "",
        "env": "AGENT_CLI_THINKING",
        "kind": "choice",
        "choices": ["off", "on", "auto"],
        "default": "off",
        "ui": {
            "icon": "",
            "group": "thinking",
            "unit": "",
            "choice_labels": {},
        },
    },
    {
        "name": "max_agents",
        "label": "Agent cap",
        "help": "",
        "flag": "--max-agents",
        "short": "",
        "env": "AGENT_CLI_MAX_AGENTS",
        "kind": "number",
        "choices": None,
        "default": 10,
        "ui": {"icon": "👥", "unit": "", "choice_labels": {}},
    },
    {
        "name": "stall",
        "label": "Stream stall limit",
        "help": "",
        "flag": "--stall",
        "short": "",
        "env": "AGENT_CLI_STREAM_IDLE_TIMEOUT_S",
        "kind": "duration",
        "choices": None,
        "default": 600,
        "ui": {
            "icon": "",
            "group": "stall",
            "unit": "min · 0 = off",
            "choice_labels": {},
        },
    },
    {
        "name": "auditor",
        "label": "Session audit",
        "help": "",
        "flag": "--auditor",
        "short": "",
        "env": "AGENT_CLI_AUDITOR",
        "kind": "bool",
        "choices": None,
        "default": False,
        "ui": {"icon": "🔎", "unit": "", "choice_labels": {}},
    },
)


@pytest.fixture(autouse=True)
def fake_knob_spec(monkeypatch):
    from agent_board import knobs_spec

    monkeypatch.setattr(knobs_spec, "load", lambda agent_cli_bin: FAKE_SPEC)
    return FAKE_SPEC
