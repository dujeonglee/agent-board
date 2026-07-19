"""테스트 전역 설정."""

from __future__ import annotations

import os

# 실브라우저 스위트(tests/browser)는 옵트인 아니면 수집조차 안 한다 —
# per-item skip 만으로는 pytest-asyncio 가 수집 단계 이벤트루프를 남겨
# 이후 async 테스트를 깨뜨린다(agent-cli 에서 실측). 로컬/수동 실행 전용
# (이 저장소는 아직 CI 없음): AGENT_BOARD_BROWSER_TESTS=1 pytest tests/browser/
if os.environ.get("AGENT_BOARD_BROWSER_TESTS") != "1":
    collect_ignore = ["browser"]
