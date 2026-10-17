"""agent-cli 노브 표 (`agent-cli knobs --json`) — 보드의 spawn 인자·생성 폼의 소스
(DESIGN §17, v1.36.0, agent-cli ≥ 10.45.0 짝).

보드는 노브 이름·플래그를 손으로 적지 않는다. agent-cli 가 자기 표를 JSON 으로
내주고(이름·플래그·env·종류·선택지·기본값·칩 모양), 보드는 그걸로 ① 새 글
폼의 컨트롤을 그리고 ② 저장된 ``Post.knobs`` 를 spawn 인자로 바꾼다. 노브가
agent-cli 에 늘면 보드의 폼과 인자도 같이 는다.

표는 프로세스당 한 번 읽는다(``cli_version`` 과 같은 lru_cache) — 바이너리는
프로세스 중 안 바뀐다. 10.45.0 미만 agent-cli 는 ``knobs`` 서브커맨드가 없어
빈 표가 되고, 그때는 모델만(``--model``) 넘긴다(종전 동작).
"""

from __future__ import annotations

import functools
import json
import re
import subprocess

_KINDS = ("text", "choice", "bool", "number", "duration")
_TRUE = {"1", "true", "on", "yes"}
_FALSE = {"0", "false", "off", "no"}
#: ``agent-cli knobs`` 가 없는 agent-cli 에서도 모델은 넘겨야 한다 — 표의 모델
#: 행과 같은 모양(이름·플래그·종류)만 든 최소 표.
_MODEL_ONLY = [
    {
        "name": "model",
        "label": "model",
        "help": "",
        "flag": "--model",
        "short": "-m",
        "env": "AGENT_CLI_MODEL",
        "kind": "text",
        "choices": None,
        "default": None,
        "ui": {},
    }
]


@functools.lru_cache(maxsize=8)
def load(agent_cli_bin: str) -> tuple[dict, ...]:
    """``<agent_cli_bin> knobs --json`` 의 표 — 실패(바이너리 부재·구버전·JSON
    아님)면 모델만 든 최소 표. 튜플(불변)이라 캐시가 안전하다."""
    try:
        out = subprocess.run(
            [agent_cli_bin, "knobs", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        spec = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        spec = None
    if not isinstance(spec, list) or not all(
        isinstance(k, dict) and k.get("name") and k.get("flag") for k in spec
    ):
        return tuple(_MODEL_ONLY)
    return tuple(spec)


def is_full(spec) -> bool:
    """표가 agent-cli 에서 왔는가(모델만 든 최소 표가 아닌가) — 프런트가 ⚙ 고급
    폼을 보일지 정한다."""
    return len(spec) > 1


def by_name(spec) -> dict[str, dict]:
    return {k["name"]: k for k in spec}


def _parse_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"expected on/off, got {v!r}")


def validate(spec, knobs: dict | None) -> dict:
    """사용자가 준 ``{이름: 값}`` 을 표로 검증해 저장 모양으로 돌려준다.

    빈 값(None·"")은 "기본값" 이라 키를 뺀다. 표에 없는 이름, 선택지 밖의
    값, 숫자가 아닌 숫자 노브는 ``ValueError`` — 서버가 400 으로 돌려준다. 값의
    **의미** 검증(범위·모델 존재)은 agent-cli 의 몫이다: 보드는 모양만 본다."""
    table = by_name(spec)
    out: dict = {}
    for name, value in (knobs or {}).items():
        if name not in table:
            raise ValueError(f"unknown knob '{name}'")
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        k = table[name]
        kind = k.get("kind")
        if kind == "bool":
            out[name] = _parse_bool(value)
        elif kind == "choice":
            v = str(value).strip().lower()
            if k.get("choices") and v not in k["choices"]:
                raise ValueError(
                    f"{name}: expected one of {', '.join(k['choices'])}, got {value!r}"
                )
            out[name] = v
        elif kind == "number":
            try:
                num = float(value)
            except (TypeError, ValueError) as e:
                raise ValueError(f"{name}: expected a number, got {value!r}") from e
            out[name] = int(num) if num.is_integer() else num
        elif kind == "duration":
            s = str(value).strip()
            if not re.fullmatch(r"\d+[smh]?", s):
                raise ValueError(f"{name}: expected 600 | 10m | 0, got {value!r}")
            out[name] = int(s) if s.isdigit() else s
        else:  # text
            out[name] = str(value).strip()
    return out


def spawn_args(spec, knobs: dict | None) -> list[str]:
    """저장된 노브 → ``agent-cli web`` 인자. 표 순서대로, 값이 있는 노브만.
    bool 은 ``--x`` / ``--no-x`` 쌍(agent-cli 의 typer 스위치), 나머지는
    ``--flag 값``. 표에 없는 이름은 조용히 건너뛴다(구버전 agent-cli 가 모르는
    노브를 받아 부트에 실패하지 않게)."""
    knobs = knobs or {}
    args: list[str] = []
    for k in spec:
        name = k["name"]
        if name not in knobs or knobs[name] is None:
            continue
        value = knobs[name]
        flag = k["flag"]
        if k.get("kind") == "bool":
            args.append(flag if _parse_bool(value) else "--no-" + flag.lstrip("-"))
        else:
            args += [flag, str(value)]
    return args
