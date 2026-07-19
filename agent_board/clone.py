"""대화방 clone — 원본 post 워크스페이스에서 선택한 파일/폴더를 새 post
워크스페이스로 복사 (v1.20.0).

두 순수 함수로 분리해 유닛 테스트 가능하게 한다:

- :func:`list_tree` — 원본 워크스페이스 한 레벨 디렉토리 목록(프런트 트리
  피커용). board 가 workspaces_root 하위 fs 를 직접 읽는다 (인스턴스가
  떠 있지 않아도 동작).
- :func:`clone_paths` — 선택된 rel 경로들을 dst 로 복사. `.agent-cli/
  sessions/<sid>/` 가 포함되면 **세션 remap**: 새 sid 로 rename +
  session.jsonl `_meta`(session_id·workspace) 재작성 + stale 사이드카
  (web.json·status.json·instance.log) 제외. 반환값에 새 sid(있으면)를
  실어 호출부가 `store.set_session_id` 로 첫 open 시 --resume 되게 한다.

경로 안전: 모든 rel 은 src/dst 하위로 강제(traversal 차단) — 세그먼트
단위 `..`/절대/백슬래시 거부 + 최종 resolve 재검증 (board 삭제 가드와
동형).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

# 복사에서 항상 제외 — 인스턴스 라이브 상태 사이드카. stale pid/port/token
# 이 새 방을 죽은 인스턴스로 오판하게 만든다(live_state/pid_alive).
_SIDECAR_EXCLUDE = {"web.json", "status.json", "instance.log"}


def _safe_join(root: Path, rel: str) -> Path:
    """``root`` 하위로만 해석되는 절대 경로. traversal 이면 ValueError."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or rel.startswith("/"):
        raise ValueError(f"unsafe path: {rel!r}")
    target = (root / Path(*parts)).resolve()
    root_r = root.resolve()
    if root_r != target and root_r not in target.parents:
        raise ValueError(f"path escapes root: {rel!r}")
    return target


def list_tree(workspace: Path, rel: str = "") -> list[dict]:
    """``workspace/rel`` 한 레벨 목록 — dirs-first, `{name, rel, type, size}`.
    디렉토리 size 는 재귀 합산. 존재하지 않으면 빈 목록."""
    base = _safe_join(workspace, rel) if rel else workspace.resolve()
    if not base.is_dir():
        return []
    entries: list[dict] = []
    for child in base.iterdir():
        crel = f"{rel}/{child.name}" if rel else child.name
        if child.is_dir():
            entries.append(
                {
                    "name": child.name,
                    "rel": crel,
                    "type": "dir",
                    "size": _dir_size(child),
                }
            )
        else:
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            entries.append(
                {"name": child.name, "rel": crel, "type": "file", "size": size}
            )
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return entries


def _dir_size(d: Path) -> int:
    total = 0
    try:
        for f in d.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def clone_paths(
    src_ws: Path,
    dst_ws: Path,
    paths: list[str],
    *,
    new_session_id: str,
) -> str | None:
    """선택 ``paths`` 를 ``src_ws`` → ``dst_ws`` 로 복사.

    복사된 것 중 `.agent-cli/sessions/<sid>/` 가 있으면 그 세션 dir 을
    ``new_session_id`` 로 rename + `_meta` 재작성 + 사이드카 제외하고,
    새 sid 를 반환한다(호출부가 store.set_session_id 로 --resume 배선).
    세션 dir 이 없으면 ``None`` (파일만 복사한 fresh 방).

    한 post = 한 세션 불변식: 여러 세션 dir 이 복사되면 첫(정렬상) 것만
    remap 하고 나머지는 그대로 둔다(실사용상 원본이 하나뿐이라 미발생).
    """
    src_ws = src_ws.resolve()
    dst_ws = dst_ws.resolve()
    for rel in paths:
        s = _safe_join(src_ws, rel)
        d = _safe_join(dst_ws, rel)
        if not s.exists():
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            shutil.copytree(
                s,
                d,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(*_SIDECAR_EXCLUDE),
            )
        else:
            if s.name in _SIDECAR_EXCLUDE:
                continue
            shutil.copy2(s, d)

    return _remap_session(dst_ws, new_session_id)


def _remap_session(dst_ws: Path, new_sid: str) -> str | None:
    """복사된 `.agent-cli/sessions/<old>/` 를 새 sid 로 rename + _meta
    재작성. 세션 dir 없으면 None."""
    sessions_dir = dst_ws / ".agent-cli" / "sessions"
    if not sessions_dir.is_dir():
        return None
    session_dirs = sorted(p for p in sessions_dir.iterdir() if p.is_dir())
    if not session_dirs:
        return None
    old = session_dirs[0]
    new = sessions_dir / new_sid
    if old.name != new_sid:
        old.rename(new)
    # 사이드카가 dir-복사 ignore 를 우회해 들어왔을 경우 대비 재삭제.
    for name in _SIDECAR_EXCLUDE:
        p = new / name
        if p.exists():
            p.unlink()
    # session.jsonl _meta 재작성 (session_id + workspace 절대경로).
    meta_path = new / "session.jsonl"
    if meta_path.is_file():
        _rewrite_meta(meta_path, new_sid, str(dst_ws))
    return new_sid


def _rewrite_meta(meta_path: Path, new_sid: str, workspace: str) -> None:
    """session.jsonl 첫 줄(_meta)의 session_id·workspace 를 갱신. 첫 줄만
    헤더이고 나머지는 대화 레코드라 그대로 둔다(경로 임베드는 표시용)."""
    lines = meta_path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError:
        return
    meta = header.get("_meta")
    if isinstance(meta, dict):
        meta["session_id"] = new_sid
        meta["workspace"] = workspace
        lines[0] = json.dumps(header, ensure_ascii=False)
        meta_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
