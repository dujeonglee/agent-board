"""List selectable models from agent-cli's ``models.json`` registry (DESIGN §8).

The board does NOT manage model definitions/keys — those live in agent-cli's
registry (``~/.agent-cli/models.json``, admin-managed). The board only reads the
ids to populate the new-post dropdown, stores the chosen id per post, and passes
it as ``--model <id>`` on spawn (agent-cli resolves provider/url/key itself).

v1.33.0 (agent-cli v10.3.0/v10.4.0 짝): 방언(dialect) 바인딩이 없는 모델은
agent-cli 가 부트/spawn 에서 거부한다 — 보드도 같은 판정(``binding_of``)으로
방 쪽 선택지에서 빼고(어드민은 전부 보여 설정하게 한다) 열기·생성을 막는다.
바인딩은 entry 의 ``dialect`` 키뿐이다(옛 ``wire_format`` 은 agent-cli
v10.4.0 부터 읽지 않는다).
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_MODELS_JSON = Path.home() / ".agent-cli" / "models.json"


def binding_of(entry) -> str | None:
    """entry 의 방언 바인딩 — ``dialect`` 가 비어 있지 않은 문자열일 때만."""
    if not isinstance(entry, dict):
        return None
    d = entry.get("dialect")
    return d if isinstance(d, str) and d else None


def _entries(path: str | Path | None) -> dict:
    path = Path(path) if path else DEFAULT_MODELS_JSON
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, dict) else {}


def list_models(path: str | Path | None = None) -> list[dict]:
    """``[{id, provider, context_window, dialect}]`` sorted by id
    (case-insensitive). ``dialect`` None = 미설정(실행 불가). Empty list if
    the registry is missing/corrupt."""
    out = [
        {
            "id": mid,
            "provider": (meta or {}).get("provider"),
            "context_window": (meta or {}).get("context_window"),
            "dialect": binding_of(meta),
        }
        for mid, meta in _entries(path).items()
    ]
    return sorted(out, key=lambda m: m["id"].lower())


def model_binding(path: str | Path | None, model_id: str | None) -> str | None:
    """``model_id`` 의 방언 바인딩 (미등록·미설정이면 None) — 서버 쪽 실행
    게이트의 판정 함수. 매 호출마다 파일을 읽는다(어드민 편집 즉시 반영)."""
    if not model_id:
        return None
    return binding_of(_entries(path).get(model_id))
