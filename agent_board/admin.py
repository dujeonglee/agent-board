"""Admin page domain logic — agent-cli ``config.json`` / ``models.json`` 편집.

board 는 원래 models.json 을 읽기만 했지만(models_registry), 어드민 페이지는
**의도적으로** 그 경계를 넘어 편집 표면이 된다 (2026-07-12 사용자 결정).
전송 계층(app.py 라우트)과 분리된 순수 도메인 모듈 — FastAPI import 없음.

설계 요점:
- ``config.json`` 의 ``api_key`` 는 GET 에서 ``***`` 로 마스킹, PUT 에서
  ``***``/빈값이면 기존 값 유지 (평문 키가 브라우저로 나가지 않음).
- 알려진 4필드(provider/base_url/api_key/default_model)만 폼 편집 대상 —
  그 외 키는 손대지 않고 보존.
- 모델 목록은 config 의 endpoint 에 ``GET /models`` 프로브를 합쳐
  served / missing / new 로 분류. **삭제는 항상 사용자 클릭** (자동 정리
  없음 — 파괴적 동작).
- capability 자동 탐지는 agent-cli 의 실제 탐지기를 lazy import 재사용
  (board 는 agent-cli 를 spawn 하는 호스트에서 돌므로 co-install 전제;
  import 실패는 친절한 에러로).
- 쓰기는 전부 원자적(temp+``os.replace``, 유니크 tmp — agent-cli
  v4.27.1 레이스 교훈): agent-cli 인스턴스의 auto-detect 저장과 동시에
  일어나도 반파일이 없다.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import httpx

DEFAULT_CONFIG_JSON = Path.home() / ".agent-cli" / "config.json"

# 폼 편집 대상 — 이 외 키는 PUT 에서 무조건 보존.
CONFIG_FORM_FIELDS = ("provider", "base_url", "api_key", "default_model")
KEY_MASK = "***"


class AdminError(Exception):
    """사용자에게 그대로 보여줄 수 있는 실패 (HTTP 400/502 감)."""


# ── 원자적 JSON I/O ──────────────────────────────────────────────


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        raise AdminError(f"{path.name} 읽기 실패: {e}") from e
    if not isinstance(data, dict):
        raise AdminError(f"{path.name} 최상위가 object 가 아닙니다")
    return data


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── config.json ──────────────────────────────────────────────────


def get_config(path: Path = DEFAULT_CONFIG_JSON) -> dict:
    """폼 뷰 — 4필드(api_key 마스킹) + 존재 여부. 파일 부재는 빈 폼."""
    cfg = _read_json(path)
    return {
        "path": str(path),
        "exists": bool(cfg),
        "provider": cfg.get("provider", ""),
        "base_url": cfg.get("base_url", ""),
        "api_key": KEY_MASK if cfg.get("api_key") else "",
        "default_model": cfg.get("default_model", ""),
    }


def update_config(fields: dict, path: Path = DEFAULT_CONFIG_JSON) -> dict:
    """4필드만 갱신 — 그 외 키 보존, api_key ``***``/빈값은 기존 유지.

    (빈 api_key 로 '키 제거'는 지원하지 않음 — 마스킹과 구분 불가.
    키 삭제가 필요하면 파일을 직접 편집.)
    """
    cfg = _read_json(path)
    for k in CONFIG_FORM_FIELDS:
        if k not in fields:
            continue
        v = fields[k]
        if not isinstance(v, str):
            raise AdminError(f"'{k}' 는 문자열이어야 합니다")
        v = v.strip()
        if k == "api_key" and v in ("", KEY_MASK):
            continue  # 기존 키 유지
        cfg[k] = v
    _atomic_write_json(path, cfg)
    return get_config(path)


# ── 서빙 중 모델 프로브 ──────────────────────────────────────────


def list_served_models(cfg_path: Path = DEFAULT_CONFIG_JSON) -> list[str]:
    """config 의 endpoint 에서 실제 서빙 중인 모델 id 목록.

    OpenAI 호환: ``GET {base_url}/models`` (Bearer) — omlx/vLLM/LM Studio
    공통. Anthropic: ``GET {base_url}/v1/models`` (x-api-key).
    """
    cfg = _read_json(cfg_path)
    base_url = (cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        raise AdminError("config.json 에 base_url 이 없습니다")
    provider = cfg.get("provider", "openai")
    api_key = cfg.get("api_key", "")
    if provider == "anthropic":
        url = f"{base_url}/v1/models"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    else:
        url = f"{base_url}/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        r = httpx.get(url, headers=headers, timeout=10.0)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        raise AdminError(f"모델 목록 프로브 실패 ({url}): {e}") from e
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise AdminError(f"모델 목록 응답 형식이 예상과 다릅니다 ({url})")
    return sorted(
        {m.get("id") for m in items if isinstance(m, dict) and m.get("id")},
        key=str.lower,
    )


# ── models.json ──────────────────────────────────────────────────


def list_models_with_status(
    models_path: Path, cfg_path: Path = DEFAULT_CONFIG_JSON
) -> dict:
    """레지스트리 + 서빙 프로브의 병합 뷰.

    ``models``: 레지스트리의 각 entry + ``status`` (``served`` — 서버에
    있음 / ``missing`` — 서버에서 사라짐 / ``unknown`` — 프로브 실패).
    ``new``: 서버에는 있지만 레지스트리에 없는 id 들 (auto-detect 후보).
    ``probe_error``: 프로브 실패 사유 (성공 시 빈 문자열) — 프로브가
    죽어도 레지스트리 편집은 계속 가능해야 하므로 예외를 삼켜 담는다.
    """
    registry = _read_json(models_path)
    models = registry.get("models")
    models = models if isinstance(models, dict) else {}
    served: list[str] = []
    probe_error = ""
    try:
        served = list_served_models(cfg_path)
    except AdminError as e:
        probe_error = str(e)
    served_set = set(served)
    rows = []
    for mid, entry in sorted(models.items(), key=lambda kv: kv[0].lower()):
        if probe_error:
            status = "unknown"
        else:
            status = "served" if mid in served_set else "missing"
        rows.append({"id": mid, "entry": entry or {}, "status": status})
    new = [] if probe_error else [m for m in served if m not in models]
    return {"models": rows, "new": new, "probe_error": probe_error}


def save_model_entry(model_id: str, entry: dict, models_path: Path) -> None:
    """entry 생성/교체 — 나머지 모델·최상위 키(provider_defaults 등) 보존."""
    if not model_id or not isinstance(entry, dict):
        raise AdminError("model id 와 entry(object)가 필요합니다")
    registry = _read_json(models_path)
    models = registry.setdefault("models", {})
    if not isinstance(models, dict):
        raise AdminError("models.json 의 'models' 가 object 가 아닙니다")
    models[model_id] = entry
    _atomic_write_json(models_path, registry)


def delete_model_entry(model_id: str, models_path: Path) -> bool:
    """레지스트리에서 제거 — 있었으면 True."""
    registry = _read_json(models_path)
    models = registry.get("models")
    if not isinstance(models, dict) or model_id not in models:
        return False
    del models[model_id]
    _atomic_write_json(models_path, registry)
    return True


# ── wire format 바인딩 (agent-cli 재사용) ────────────────────────


def list_wire_format_names() -> list[str]:
    """agent-cli 의 등록 wire format 이름 목록 — 바인딩 드롭다운 옵션.

    바인딩(models.json entry 의 선택 필드 ``wire_format``)은 agent-cli
    v5.19.0 해석 체인의 소스: 그 모델(main·서브에이전트)이 어떤 응답
    포맷으로 돌지 정한다. 드롭다운은 **등록명만** 제공 (자유입력 금지 —
    agent-cli 부트가 unknown 이름에 fail-fast 하므로 오타를 UI 에서 원천
    차단). lazy import 는 ``detect_model_entry`` 동형; 미설치면 빈 목록 —
    UI 는 auto + 현재값 보존만 제공한다.
    """
    try:
        from agent_cli.wire_formats import list_names
    except ImportError:
        return []
    return list_names()


# ── capability 자동 탐지 (agent-cli 재사용) ──────────────────────


def detect_model_entry(model_id: str, cfg_path: Path = DEFAULT_CONFIG_JSON) -> dict:
    """agent-cli 의 런타임 capability 탐지기로 entry 초안을 만든다.

    저장하지 않는다 — 호출자(UI)가 검토·수정 후 PUT 으로 저장.
    agent_cli 미설치(비정상 배포)면 친절한 에러. 수 초~수십 초 걸리는
    블로킹 호출 — 전송 계층이 executor 로 오프로드할 것.
    """
    try:
        from agent_cli.providers.capabilities import (
            _detect_runtime_capabilities,
            caps_to_entry,
        )
    except ImportError as e:
        raise AdminError(
            "agent_cli 패키지를 import 할 수 없어 자동 탐지를 못 합니다 — "
            "수동 입력으로 저장하세요"
        ) from e

    cfg = _read_json(cfg_path)
    base_url = cfg.get("base_url", "")
    if not base_url:
        raise AdminError("config.json 에 base_url 이 없습니다")
    caps = _detect_runtime_capabilities(
        cfg.get("provider", "openai"), base_url, model_id, cfg.get("api_key", "")
    )
    if caps is None:
        raise AdminError(f"'{model_id}' capability 탐지 실패 (프로브 무응답/거부)")
    return caps_to_entry(caps, auto_detected=True)
