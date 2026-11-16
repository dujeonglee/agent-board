"""Core data types.

A ``Post`` is the persistent record of a board post. Its workspace is NOT
stored — it is derived from ``post_id`` under the configured workspaces root
(see ``config.Config.workspace_for``), so there is no user-supplied path and no
shared-workspace collisions. Ephemeral state (port/token/status/last_query) is
never stored here; it is read live from the instance's ``web.json`` + session
files.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Post:
    post_id: str
    topic: str
    session_id: str | None = None  # filled on first open (agent-cli session)
    # agent-cli 세션 노브 ``{이름: 값}`` (v1.36.0) — 모델(``knobs["model"]``)을
    # 포함한다. 생성 때 한 번 받고, 그 뒤엔 방이 보고하는 값(status.json
    # ``knobs``)을 거울처럼 기록한다. 다음 spawn 은 이걸 표의 플래그로 넘긴다.
    knobs: dict = field(default_factory=dict)
    # 모델 서버 이름 (v1.48.0, agent-cli ≥ 10.144.0 의 config.json ``endpoints``) — 생성 폼에서
    # 한 번 고르고(기본 = 그때의 기본 서버), 방이 뜬 뒤에는 바꾸지 않는다(거울 규칙; 다른
    # 서버는 복제로). 빈 문자열 = 기록 없음(v1.48.0 전 글) — 그 방이 뜨면 web.json 에서 배운다.
    endpoint: str = ""
    force_active: bool = False
    created_at: str = ""
    last_opened_at: str | None = None

    @property
    def model(self) -> str | None:
        return self.knobs.get("model") or None
