"""agent-cli 의 서버 표(config.json)와 서버별 모델 표(models.json) 읽기 (DESIGN §8).

The board does NOT manage model definitions/keys here — those live in agent-cli's
files (``~/.agent-cli/config.json``·``models.json``, admin-managed). This module
only reads: the saved server names (new-post form's Server select), and per
server the model ids (Model select) and their dialect bindings (the run gate).

모양 (agent-cli ≥ 10.144.0)::

    config.json  {"endpoint": "<기본 서버>", "endpoints": {"<이름>": {...}}}
    models.json  {"endpoints": {"<이름>": {"models": {"<id>": {...}}}}}

옛 모양(최상위에 ``endpoints`` 가 없는 파일)은 agent-cli 처럼 **읽지 않는다** — 서버 없음.
v1.33.0: 방언(dialect) 바인딩이 없는 모델은 agent-cli 가 부트에서 거부한다 — 보드도 같은
판정(``binding_of``)으로 방 쪽 선택지에서 빼고 열기·생성을 막는다. 매 호출 파일을 읽는다
(어드민 편집이 곧바로 보인다).
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_MODELS_JSON = Path.home() / ".agent-cli" / "models.json"
DEFAULT_CONFIG_JSON = Path.home() / ".agent-cli" / "config.json"


def binding_of(entry) -> str | None:
    """entry 의 방언 바인딩 — ``dialect`` 가 비어 있지 않은 문자열일 때만."""
    if not isinstance(entry, dict):
        return None
    d = entry.get("dialect")
    return d if isinstance(d, str) and d else None


def _read(path: str | Path | None, default: Path) -> dict:
    """새 모양 파일 하나 — 없거나·깨졌거나·옛 모양이면 {}."""
    path = Path(path) if path else default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict) or "endpoints" not in data:
        return {}
    return data


def _sections(data: dict) -> dict:
    eps = data.get("endpoints")
    return eps if isinstance(eps, dict) else {}


def server_names(cfg_path: str | Path | None) -> tuple[list[str], str | None]:
    """``(저장된 서버 이름들, 기본 서버 이름)`` — 기본은 저장된 이름일 때만(없으면 None)."""
    data = _read(cfg_path, DEFAULT_CONFIG_JSON)
    names = sorted(n for n, v in _sections(data).items() if isinstance(v, dict))
    default = data.get("endpoint")
    return names, default if isinstance(default, str) and default in names else None


def endpoint_host(base_url: str) -> str:
    """주소에서 ``host[:port]`` 만 — 경로·쿼리·사용자 정보는 버린다(토큰을 쿼리나 userinfo 에
    싣는 게이트웨이가 있다). agent-cli ``config.endpoint_host`` 와 같은 규칙(계약 테스트가 묶는다)
    이지만 stdlib 만 쓴다 — 새 글 폼(``/api/endpoints``)은 agent-cli 를 import 할 수 없는 배포
    (다른 venv, ``agent_cli_bin`` 으로만 이어짐)에서도 돌아야 한다."""
    try:
        parts = urlsplit(base_url or "")
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}" if host and port else host


def list_servers(cfg_path: str | Path | None) -> dict:
    """새 글 폼의 Server 선택지 — ``{"default", "servers": [{name, host, default, default_model}]}``.

    주소는 ``host[:port]`` 만(:func:`endpoint_host`) — 경로·쿼리·사용자 정보는 내주지 않는다
    (쿼리에 토큰을 싣는 게이트웨이가 있다). 키·헤더는 어떤 값도 없다. agent-cli 를 import 하지
    않는다."""
    data = _read(cfg_path, DEFAULT_CONFIG_JSON)
    names, default = server_names(cfg_path)
    sections = _sections(data)
    return {
        "default": default,
        "servers": [
            {
                "name": n,
                "host": endpoint_host(str(sections[n].get("base_url") or "")),
                "default": n == default,
                # 새 글 폼이 서버를 바꿀 때 미리 고를 모델 — 비밀이 아니다
                "default_model": str(sections[n].get("default_model") or ""),
            }
            for n in names
        ],
    }


def _entries(models_path: str | Path | None, endpoint: str | None) -> dict:
    if not endpoint:
        return {}
    section = _sections(_read(models_path, DEFAULT_MODELS_JSON)).get(endpoint)
    models = section.get("models") if isinstance(section, dict) else None
    return models if isinstance(models, dict) else {}


def list_models(models_path: str | Path | None, endpoint: str | None) -> list[dict]:
    """서버 ``endpoint`` 의 ``[{id, provider, context_window, dialect}]`` — id 순(대소문자 무시).
    ``dialect`` None = 미설정(실행 불가). 표가 없거나 깨졌으면 빈 목록."""
    out = [
        {
            "id": mid,
            "provider": (meta or {}).get("provider"),
            "context_window": (meta or {}).get("context_window"),
            "dialect": binding_of(meta),
        }
        for mid, meta in _entries(models_path, endpoint).items()
    ]
    return sorted(out, key=lambda m: m["id"].lower())


def model_binding(
    models_path: str | Path | None, endpoint: str | None, model_id: str | None
) -> str | None:
    """서버 ``endpoint`` 에서 ``model_id`` 의 방언 바인딩 (미등록·미설정이면 None) — 서버 쪽
    실행 게이트의 판정 함수."""
    if not model_id:
        return None
    return binding_of(_entries(models_path, endpoint).get(model_id))
