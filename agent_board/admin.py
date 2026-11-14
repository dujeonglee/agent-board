"""Admin page domain logic — agent-cli ``config.json`` / ``models.json`` 편집.

board 는 원래 models.json 을 읽기만 했지만(models_registry), 어드민 페이지는
**의도적으로** 그 경계를 넘어 편집 표면이 된다 (2026-07-12 사용자 결정).
전송 계층(app.py 라우트)과 분리된 순수 도메인 모듈 — FastAPI import 없음.

설계 요점:
- ``config.json`` 의 ``api_key`` 는 GET 에서 ``***`` 로 마스킹, PUT 에서
  ``***``/빈값이면 기존 값 유지 (평문 키가 브라우저로 나가지 않음).
- 알려진 4필드(provider/base_url/api_key/default_model)만 폼 편집 대상 —
  그 외 키는 손대지 않고 보존.
- ``extra_headers`` (v1.47.0): 모델 서버로 가는 요청에 붙는 사용자 헤더 목록.
  값은 api_key 처럼 GET 에서 ``***``, PUT 에서 ``***`` 면 같은 이름의 기존 값 유지.
  검증·요청 헤더 만들기는 agent-cli 의 것을 그대로 쓴다(규칙 하나 —
  ``agent_cli.providers.credentials``).
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
import sys
import tempfile
from pathlib import Path

import httpx

from agent_board import models_registry

DEFAULT_CONFIG_JSON = Path.home() / ".agent-cli" / "config.json"

# 폼 편집 대상 — 이 외 키는 PUT 에서 무조건 보존.
CONFIG_FORM_FIELDS = ("provider", "base_url", "api_key", "default_model")
KEY_MASK = "***"


def base_url_is_remote(base_url: str) -> bool:
    """True if ``base_url`` points at a non-loopback host — i.e. probing it
    ships the stored API key off-box.

    The models probe legitimately sends the key to the configured endpoint (a
    real remote LLM server needs it), so we cannot refuse remote targets
    without breaking normal use. But an attacker who can reach the admin API
    (closed remotely by AUDIT B-1's loopback bind) could repoint ``base_url``
    to exfiltrate the key; surfacing WHERE the key goes is the residual
    defense-in-depth. Full insider mitigation needs per-user perms (v2)."""
    from urllib.parse import urlparse

    host = (urlparse(base_url).hostname or "").lower()
    if not host:
        return False
    if host in ("localhost",):
        return False
    try:
        import ipaddress

        ip = ipaddress.ip_address(host)
        return not (ip.is_loopback or ip.is_private)
    except ValueError:
        # A hostname (not an IP literal) that isn't localhost → treat as remote.
        return True


class AdminError(Exception):
    """사용자에게 그대로 보여줄 수 있는 실패 (HTTP 400/502 감)."""


def _credentials_module():
    """agent-cli 의 자격 모듈 — 추가 헤더 검증·요청 헤더의 단일 출처.

    import 불가(비정상 배포)면 탐지 경로와 같은 친절한 AdminError."""
    import importlib

    try:
        credentials = importlib.import_module("agent_cli.providers.credentials")
    except ImportError as e:
        raise AdminError(
            "the agent_cli package cannot be imported, so extra headers and the "
            "model list probe are unavailable"
        ) from e
    return credentials


def _credentials(cfg: dict):
    """config dict → agent-cli ``Credentials`` (규칙 위반은 이름만 담은 AdminError)."""
    cred = _credentials_module()
    try:
        return cred.credentials_from_config(cfg)
    except cred.ExtraHeaderError as e:
        raise AdminError(f"config.json extra_headers: {e}") from e


def _extra_headers_view(raw) -> list[dict]:
    """GET 용 — 값은 ``***``(빈 값은 빈 문자열). 손으로 적은 이상한 항목도 보이게
    관대하게 읽는다(검증은 저장·사용 때)."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        enabled = item.get("enabled", True)
        out.append(
            {
                "name": str(item.get("name", "")),
                "value": KEY_MASK if item.get("value") else "",
                "enabled": enabled if isinstance(enabled, bool) else True,
            }
        )
    return out


def _resolve_extra_headers(incoming, existing) -> list:
    """PUT 의 ``extra_headers`` — ``***`` 값을 같은 이름(대소문자 무시)의 기존 값으로.

    같은 이름이 여럿 저장돼 있으면 나온 순서대로 짝짓는다. 기존에 없는 이름인데
    ``***`` 면 오류(지킬 값이 없다). 검증은 호출자가 agent-cli 규칙으로 한다."""
    if not isinstance(incoming, list):
        raise AdminError("'extra_headers' must be a list")
    saved: dict[str, list[str]] = {}
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                saved.setdefault(item["name"].lower(), []).append(item.get("value", ""))
    out = []
    for i, item in enumerate(incoming):
        if not isinstance(item, dict):
            raise AdminError(f"extra_headers[{i}] must be an object")
        item = dict(item)
        name = item.get("name")
        if isinstance(name, str):
            item["name"] = name = name.strip()
        if item.get("value") == KEY_MASK:
            pool = saved.get(name.lower(), []) if isinstance(name, str) else []
            if not pool:
                raise AdminError(
                    f"extra header {name!r} has no saved value to keep — enter a value"
                )
            item["value"] = pool.pop(0)
        out.append(item)
    return out


# ── 원자적 JSON I/O ──────────────────────────────────────────────


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        raise AdminError(f"could not read {path.name}: {e}") from e
    if not isinstance(data, dict):
        raise AdminError(f"{path.name}: top level is not an object")
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
        "extra_headers": _extra_headers_view(cfg.get("extra_headers")),
    }


def update_config(fields: dict, path: Path = DEFAULT_CONFIG_JSON) -> dict:
    """4필드 + ``extra_headers`` 만 갱신 — 그 외 키 보존, api_key ``***``/빈값은 기존 유지.

    (빈 api_key 로 '키 제거'는 지원하지 않음 — 마스킹과 구분 불가.
    키 삭제가 필요하면 파일을 직접 편집.)

    ``extra_headers`` 는 목록 전체를 받는다(빠진 항목은 지워진다). ``***`` 값은 같은
    이름의 기존 값 유지. agent-cli 의 ``validate_extra_headers`` 를 통과해야 저장한다 —
    위반이면 파일은 그대로다.
    """
    cfg = _read_json(path)
    if "extra_headers" in fields:
        cred = _credentials_module()
        resolved = _resolve_extra_headers(
            fields["extra_headers"], cfg.get("extra_headers")
        )
        try:
            cfg["extra_headers"] = cred.validate_extra_headers(resolved)
        except cred.ExtraHeaderError as e:
            raise AdminError(str(e)) from e
    for k in CONFIG_FORM_FIELDS:
        if k not in fields:
            continue
        v = fields[k]
        if not isinstance(v, str):
            raise AdminError(f"'{k}' must be a string")
        v = v.strip()
        if k == "api_key" and v in ("", KEY_MASK):
            continue  # 기존 키 유지
        cfg[k] = v
    _atomic_write_json(path, cfg)
    return get_config(path)


# ── 서빙 중 모델 프로브 ──────────────────────────────────────────


def list_served_models(cfg_path: Path = DEFAULT_CONFIG_JSON) -> list[str]:
    """config 의 endpoint 에서 실제 서빙 중인 모델 id 목록.

    OpenAI 호환: ``GET {base_url}/models`` — omlx/vLLM/LM Studio 공통.
    Anthropic: ``GET {base_url}/v1/models``. 헤더(키·추가 헤더)는 agent-cli 의
    ``Credentials.headers`` 가 만든다 — 런타임이 보내는 것과 같은 모양.
    """
    cfg = _read_json(cfg_path)
    base_url = (cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        raise AdminError("config.json has no base_url")
    provider = cfg.get("provider", "openai")
    credentials = _credentials(cfg)
    headers = credentials.headers(provider)
    if provider == "anthropic":
        url = f"{base_url}/v1/models"
    else:
        url = f"{base_url}/models"
    secrets = _secret_summary(credentials)
    if secrets and base_url_is_remote(base_url):
        # Defense-in-depth (AUDIT B-2): make it visible that the stored key /
        # extra headers are being sent to a remote host — an attacker-repointed
        # base_url would otherwise exfiltrate them silently. (Remote is
        # legitimate for a real remote LLM, so we warn, not block.) Names and
        # counts only — never values.
        print(
            f"  ⚠️  the admin model probe sends {secrets} to a remote host: {base_url}",
            file=sys.stderr,
        )
    try:
        r = httpx.get(url, headers=headers, timeout=10.0)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        raise AdminError(f"model list probe failed ({url}): {e}") from e
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise AdminError(f"unexpected model list response shape ({url})")
    return sorted(
        {m.get("id") for m in items if isinstance(m, dict) and m.get("id")},
        key=str.lower,
    )


def _secret_summary(credentials) -> str:
    """원격 경고 문구의 "무엇을" — 값 없이 키 여부와 추가 헤더 개수만."""
    parts = []
    if credentials.api_key:
        parts.append("the API key")
    n = len(credentials.extra_headers)
    if n:
        parts.append(f"{n} extra header{'s' if n != 1 else ''}")
    return " and ".join(parts)


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
        rows.append(
            {
                "id": mid,
                "entry": entry or {},
                "status": status,
                "dialect": models_registry.binding_of(entry),
            }
        )
    new = [] if probe_error else [m for m in served if m not in models]
    return {"models": rows, "new": new, "probe_error": probe_error}


#: 🎲 모델 기본 샘플링 키의 범위 (v1.39.0, agent-cli ≥ 10.69.0 `SAMPLING_RANGES` 와 같다).
SAMPLING_RANGES: dict[str, tuple[float, float]] = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
}


def _check_sampling(entry: dict) -> None:
    """엔트리의 선택 키 ``temperature``·``top_p`` — 있으면 범위 안의 숫자여야 한다.
    None 은 "미기록" 과 같아 키를 지운다(폼의 빈 칸). 하니스는 오타를 미기록으로
    조용히 넘기지만(부트를 막지 않기 위해), 편집기는 사용자가 보고 있으니 바로 말한다."""
    for key, (lo, hi) in SAMPLING_RANGES.items():
        if key not in entry:
            continue
        value = entry[key]
        if value is None:
            del entry[key]
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AdminError(f"{key} must be a number between {lo:g} and {hi:g}")
        if not lo <= float(value) <= hi:
            raise AdminError(f"{key} must be between {lo:g} and {hi:g}, got {value}")
        entry[key] = float(value)


def save_model_entry(model_id: str, entry: dict, models_path: Path) -> None:
    """entry 생성/교체 — 나머지 모델·최상위 키(provider_defaults 등) 보존.

    v1.33.0: ``dialect`` 바인딩은 필수 — 없으면 agent-cli 가 그 모델을
    실행하지 않으므로(v10.3.0) 저장 단계에서 막는다. 이름은 agent-cli 등록
    방언이어야 한다(목록을 못 얻는 환경 — 미설치 — 에서만 이름 검증 생략).
    """
    if not model_id or not isinstance(entry, dict):
        raise AdminError("model id and entry (object) are required")
    dialect = entry.get("dialect")
    if not (isinstance(dialect, str) and dialect):
        raise AdminError(
            "a dialect binding is required — choose the reply shape this model "
            "runs with (json_fc if unsure)"
        )
    names = list_dialect_names()
    if names and dialect not in names:
        raise AdminError(f"unknown dialect '{dialect}' (one of: {', '.join(names)})")
    _check_sampling(entry)
    registry = _read_json(models_path)
    models = registry.setdefault("models", {})
    if not isinstance(models, dict):
        raise AdminError("'models' in models.json is not an object")
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


# ── 방언(dialect) 바인딩 (agent-cli 재사용) ─────────────────────


def list_dialect_names() -> list[str]:
    """agent-cli 의 등록 방언(dialect) 이름 목록 — 바인딩 드롭다운 옵션.

    바인딩(models.json entry 의 **필수** 필드 ``dialect``, v1.33.0)은 agent-cli
    해석 체인의 소스: 그 모델(main·서브에이전트)이 어떤 응답 모양으로 돌지
    정한다. 드롭다운은 **등록명만** 제공 (자유입력 금지 — agent-cli 부트가
    unknown 이름에 fail-fast 하므로 오타를 UI 에서 원천 차단). lazy import 는
    ``detect_model_entry`` 동형; 미설치면 빈 목록.
    """
    try:
        from agent_cli.dialects import list_names
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
            UnsupportedModelError,
            _detect_runtime_capabilities,
            caps_to_entry,
        )
    except ImportError as e:
        raise AdminError(
            "the agent_cli package cannot be imported, so auto-detection is "
            "unavailable — save manual values"
        ) from e

    cfg = _read_json(cfg_path)
    base_url = cfg.get("base_url", "")
    if not base_url:
        raise AdminError("config.json has no base_url")
    credentials = _credentials(cfg)
    try:
        caps = _detect_runtime_capabilities(
            cfg.get("provider", "openai"), base_url, model_id, credentials
        )
    except UnsupportedModelError as e:
        # 탐지기는 "쓸 수 없는 모델"(컨텍스트 창이 최소치 미만)을 None 이 아니라
        # 예외로 알린다 — 사유 문구를 그대로 화면에 올린다.
        raise AdminError(str(e)) from e
    if caps is None:
        raise AdminError(
            f"capability detection for '{model_id}' failed (probe unanswered or rejected)"
        )
    return caps_to_entry(caps, auto_detected=True)
