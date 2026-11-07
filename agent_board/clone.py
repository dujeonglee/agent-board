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
import os
import shutil
from collections.abc import Callable
from pathlib import Path

#: 진행 콜백 ``progress(files_done, bytes_done, current_rel)`` (v1.42.0) — 복제 작업이
#: 메모리 상태를 갱신하고 모달·카드가 250 ms 폴링으로 읽는다.
Progress = Callable[[int, int, "str | None"], None]

#: 이보다 큰 파일은 조각으로 복사하며 바를 움직인다 — 한 파일이 커도 멈춰 보이지 않게.
CHUNK = 4 * 1024 * 1024

# 복사에서 항상 제외 — 인스턴스 라이브 상태 사이드카. stale pid/port/token
# 이 새 방을 죽은 인스턴스로 오판하게 만든다(live_state/pid_alive).
# 예약(schedules.json·이력)과 세션 잠금은 원본 세션의 것이다 — 복제본이
# 물려받으면 두 방이 같은 예약을 각자 발화한다 (v1.34.0).
_SIDECAR_EXCLUDE = {
    "web.json",
    "status.json",
    "instance.log",
    "schedules.json",
    "schedule-log.jsonl",
    "session.lock",
}


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


def _lexical_join(root: Path, rel: str) -> Path:
    """``root`` 밑의 ``rel`` — :func:`_safe_join` 과 같은 세그먼트 검사(``..``·절대·
    백슬래시)를 하되 **마지막 이름은 풀지 않는다**(v1.43.0). 직접 고른 항목이 심링크면
    링크 자체를 가리켜야 하기 때문이다(``_safe_join`` 은 대상으로 풀어, 밖을 가리키는
    링크를 고르면 "path escapes root" 로 복제가 실패했다). 부모는 풀어서 root 밑인지
    다시 본다. 링크는 링크로 다시 만들 뿐 따라가지 않으니 마지막 이름이 밖을 가리켜도
    안전하다."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or rel.startswith("/"):
        raise ValueError(f"unsafe path: {rel!r}")
    if not parts:
        return root.resolve()
    parent = (root / Path(*parts[:-1])).resolve()
    root_r = root.resolve()
    if root_r != parent and root_r not in parent.parents:
        raise ValueError(f"path escapes root: {rel!r}")
    return parent / parts[-1]


def _copy_link(s: Path, d: Path) -> None:
    """링크를 대상 문자열 그대로 다시 만든다 — 내용은 복사하지 않는다 (v1.43.0).
    대상 자리에 링크가 있으면 지우고, 실제 파일·디렉터리가 있으면 ``FileExistsError`` 를
    그대로 올린다(작업이 ``failed`` 로 남는다)."""
    if d.is_symlink():
        d.unlink()
    os.symlink(os.readlink(s), d)
    try:
        shutil.copystat(s, d, follow_symlinks=False)
    except (NotImplementedError, OSError):
        pass  # 링크의 mtime 은 중요하지 않다 — 못 하는 플랫폼은 지나간다


def list_tree(workspace: Path, rel: str = "") -> list[dict]:
    """``workspace/rel`` 한 레벨 목록 — dirs-first, `{name, rel, type, size}`.
    디렉토리 size 는 재귀 합산. 존재하지 않으면 빈 목록."""
    base = _safe_join(workspace, rel) if rel else workspace.resolve()
    if not base.is_dir():
        return []
    entries: list[dict] = []
    for child in base.iterdir():
        crel = f"{rel}/{child.name}" if rel else child.name
        if child.is_symlink():
            # 링크는 링크로 복사된다(v1.43.0) — 따라가지 않고, 크기도 대상 것을 더하지
            # 않는다. 트리 피커는 🔗 로 그리고 펼치지 않는다.
            try:
                target = os.readlink(child)
            except OSError:
                target = ""
            entries.append(
                {
                    "name": child.name,
                    "rel": crel,
                    "type": "link",
                    "size": 0,
                    "target": target,
                }
            )
            continue
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
            # 링크 파일은 더하지 않는다 — 복사되는 바이트와 같게(링크는 링크로 복사된다).
            # rglob 은 링크 디렉터리 안으로는 들어가지 않는다.
            if f.is_symlink():
                continue
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def plan_paths(src_ws: Path, paths: list[str]) -> tuple[int, int]:
    """복사될 ``(파일 수, 바이트 합)`` — :func:`clone_paths` 와 **같은 규칙**으로 센다
    (v1.42.0): 사이드카 이름(``_SIDECAR_EXCLUDE``)은 어느 깊이에서든 뺀다, 없는 rel 은
    건너뛴다, 못 읽는 항목은 세지 않는다(복사 쪽이 그때 실패를 낸다). traversal 이면
    ValueError.

    심링크 (v1.43.0, B-11 닫힘): 링크는 링크로 다시 만들 뿐 내용을 복사하지 않으므로
    **세지 않는다**(파일 0·바이트 0 — 바와 총계는 "복사되는 바이트" 다). 따라가지도
    않는다. 진행 콜백도 링크를 보고하지 않아(``copytree(symlinks=True)`` 는 링크에
    ``copy_function`` 을 부르지 않는다) 계획과 실제가 정확히 같다."""
    src_ws = src_ws.resolve()
    files = total = 0

    def add_file(p: Path) -> None:
        nonlocal files, total
        try:
            size = p.stat().st_size
        except OSError:
            return
        files += 1
        total += size

    def walk(d: Path) -> None:
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            if e.name in _SIDECAR_EXCLUDE:
                continue
            p = Path(e.path)
            if p.is_symlink():  # 링크는 세지 않고 따라가지 않는다 (위)
                continue
            if p.is_dir():
                walk(p)
            else:
                add_file(p)

    for rel in paths:
        s = _lexical_join(src_ws, rel)
        if s.is_symlink():  # 직접 고른 링크도 같다
            continue
        if not s.exists():
            continue
        if s.is_dir():
            walk(s)
        elif s.name not in _SIDECAR_EXCLUDE:
            add_file(s)
    return files, total


def _copy_with_progress(src_ws: Path, report: Callable[[int, str], None]):
    """``copy2`` 와 같은 의미(내용 + ``copystat``)의 복사 함수 — 큰 파일은 ``CHUNK``
    조각으로 복사하며 ``report(bytes, rel)`` 로 진행을 알리고, 파일을 다 쓰면
    ``report(0, None)`` 으로 파일 하나 끝을 알린다."""

    def copy(src, dst):
        src = os.fspath(src)
        try:
            rel = Path(src).relative_to(src_ws).as_posix()
        except ValueError:
            rel = os.path.basename(src)
        if os.path.getsize(src) <= CHUNK:
            shutil.copy2(src, dst)
            report(os.path.getsize(dst), rel)
        else:
            with open(src, "rb") as fi, open(dst, "wb") as fo:
                while True:
                    buf = fi.read(CHUNK)
                    if not buf:
                        break
                    fo.write(buf)
                    report(len(buf), rel)
            shutil.copystat(src, dst)
        report(0, None)
        return dst

    return copy


def clone_paths(
    src_ws: Path,
    dst_ws: Path,
    paths: list[str],
    *,
    new_session_id: str,
    src_session_id: str | None = None,
    progress: Progress | None = None,
) -> str | None:
    """선택 ``paths`` 를 ``src_ws`` → ``dst_ws`` 로 복사.

    복사된 것 중 **원본 글의 세션**(``src_session_id`` — 보드가 저장해 둔
    것) dir 이 있으면 그것을 ``new_session_id`` 로 rename + `_meta` 재작성 +
    사이드카 제외하고, 새 sid 를 반환한다(호출부가 store.set_session_id 로
    --resume 배선). 원본 글에 세션이 없으면(한 번도 안 연 글) 복사된 세션
    dir 이 **정확히 하나**일 때만 그것을 쓴다 — 여럿이면 어느 것이 대화인지
    보드가 알 수 없으므로 고르지 않고 ``None`` (v1.33.1: 종전엔 정렬상 첫
    dir 을 집어, 터미널에서 따로 돌린 세션이 섞인 워크스페이스에서 엉뚱한
    세션을 이어받을 수 있었다). 세션 dir 이 없으면 ``None`` (fresh 방).

    한 post = 한 세션 불변식: 여러 세션 dir 이 복사되면 첫(정렬상) 것만
    remap 하고 나머지는 그대로 둔다(실사용상 원본이 하나뿐이라 미발생).

    복사·remap 후 `.agent-cli` 하위 모든 텍스트 파일에서 **옛 workspace
    절대경로**(`str(src_ws)`)를 새 것으로 치환한다 — history.jsonl·중첩
    agents/*/history.jsonl·_meta 등에 박힌 이전 workspace 경로가 이어받은
    대화에 새어들어 모델을 혼란시키는 것을 방지(경로 프리픽스 단위라
    워크스페이스 밖 절대경로는 안 건드림).

    ``progress`` (v1.42.0): 파일을 쓸 때마다(큰 파일은 조각마다)
    ``progress(files_done, bytes_done, current_rel)`` — 전체는 :func:`plan_paths` 가 같은
    규칙으로 미리 센다. 복사되는 집합은 콜백 유무와 무관하게 같다.
    """
    src_ws = src_ws.resolve()
    dst_ws = dst_ws.resolve()
    copy_fn = shutil.copy2
    if progress is not None:
        done = {"files": 0, "bytes": 0}

        def report(nbytes: int, rel: str | None) -> None:
            if rel is None:
                done["files"] += 1
            else:
                done["bytes"] += nbytes
            progress(done["files"], done["bytes"], rel)

        copy_fn = _copy_with_progress(src_ws, report)
    for rel in paths:
        s = _lexical_join(src_ws, rel)
        d = _lexical_join(dst_ws, rel)
        if s.is_symlink():
            # 직접 고른 항목이 링크(파일이든 디렉터리든) — `is_dir()` 보다 먼저 본다
            # (링크 dir 은 is_dir() 이 참). 깨진 링크도 링크로 (v1.43.0, B-11).
            if s.name in _SIDECAR_EXCLUDE:
                continue
            d.parent.mkdir(parents=True, exist_ok=True)
            _copy_link(s, d)
            continue
        if not s.exists():
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            # symlinks=True (v1.43.0, B-11 닫힘): 안의 링크는 대상 문자열 그대로 다시
            # 만들고 따라가지 않는다 — 종전 symlinks=False 는 링크가 가리키는 내용(밖의
            # /etc·홈 디렉터리까지)을 실제 파일로 새 방에 복사했다. 깨진 링크도 링크로
            # 만들어질 뿐 오류가 아니다. 링크에는 copy_function 이 불리지 않는다.
            shutil.copytree(
                s,
                d,
                symlinks=True,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(*_SIDECAR_EXCLUDE),
                copy_function=copy_fn,
            )
        else:
            if s.name in _SIDECAR_EXCLUDE:
                continue
            copy_fn(s, d)

    new_sid = _remap_session(dst_ws, new_session_id, src_session_id)
    _rewrite_workspace_paths(dst_ws, str(src_ws), str(dst_ws))
    return new_sid


def _rewrite_workspace_paths(dst_ws: Path, old_ws: str, new_ws: str) -> None:
    """복사된 `.agent-cli` 하위 모든 텍스트 파일에서 ``old_ws`` 절대경로를
    ``new_ws`` 로 치환. 바이너리(utf-8 미해독)·경로 미포함 파일은 스킵,
    변경분만 재기록. 프리픽스가 아니라 리터럴 부분문자열 치환이지만
    old_ws 는 완전한 절대 workspace 경로라 `/…/workspaces/<old>/sub/f` →
    `/…/workspaces/<new>/sub/f` 로 정확히 이동한다(워크스페이스 밖 경로는
    old_ws 를 포함하지 않으므로 무영향)."""
    agent_dir = dst_ws / ".agent-cli"
    if old_ws == new_ws or not agent_dir.is_dir():
        return
    for f in agent_dir.rglob("*"):
        # 링크는 열지 않는다 (v1.43.0) — 링크 너머의 밖 파일을 고쳐 쓰면 안 된다.
        if f.is_symlink() or not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # 바이너리/읽기 실패 — 건드리지 않음
        if old_ws not in text:
            continue
        f.write_text(text.replace(old_ws, new_ws), encoding="utf-8")


def _remap_session(dst_ws: Path, new_sid: str, src_sid: str | None) -> str | None:
    """복사된 `.agent-cli/sessions/<src_sid>/` 를 새 sid 로 rename + _meta
    재작성. 원본 세션이 안 넘어왔으면(또는 모르면서 dir 이 여럿이면) None."""
    sessions_dir = dst_ws / ".agent-cli" / "sessions"
    if not sessions_dir.is_dir():
        return None
    session_dirs = [p for p in sessions_dir.iterdir() if p.is_dir()]
    if src_sid:
        old = sessions_dir / src_sid
        if not old.is_dir():
            return None
    elif len(session_dirs) == 1:
        old = session_dirs[0]
    else:
        return None
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
