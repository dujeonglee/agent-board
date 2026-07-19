"""clone.py 유닛 — 트리 목록 + 세션 remap 복사 (v1.20.0)."""

from __future__ import annotations

import json

import pytest

from agent_board import clone


def _mk_session(ws, sid, *, meta_ws="/old/ws", sidecars=True):
    d = ws / ".agent-cli" / "sessions" / sid
    d.mkdir(parents=True)
    (d / "session.jsonl").write_text(
        json.dumps(
            {
                "_meta": {
                    "session_id": sid,
                    "workspace": meta_ws,
                    "updated_at": "x",
                    "response_format": "json_fc",
                }
            }
        )
        + "\n"
        + json.dumps({"role": "user", "content": "hi"})
        + "\n"
    )
    (d / "history.jsonl").write_text('{"role":"user"}\n')
    (d / "agents.json").write_text('{"version":1,"agents":[],"pending":[]}')
    if sidecars:
        (d / "web.json").write_text('{"pid":999,"port":1}')
        (d / "status.json").write_text('{"busy":false}')
        (d / "instance.log").write_text("log")
    return d


class TestSafeJoin:
    def test_traversal_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            clone._safe_join(tmp_path, "../etc/passwd")
        with pytest.raises(ValueError):
            clone._safe_join(tmp_path, "/abs")
        with pytest.raises(ValueError):
            clone._safe_join(tmp_path, "a/../../b")

    def test_normal_resolves_under_root(self, tmp_path):
        assert clone._safe_join(tmp_path, "a/b").parent.name == "a"


class TestListTree:
    def test_dirs_first_and_sizes(self, tmp_path):
        (tmp_path / "z.txt").write_text("12345")
        (tmp_path / "adir").mkdir()
        (tmp_path / "adir" / "f").write_text("xy")
        tree = clone.list_tree(tmp_path)
        assert [e["name"] for e in tree] == ["adir", "z.txt"]  # dirs first
        assert tree[0]["type"] == "dir" and tree[0]["size"] == 2
        assert tree[1]["size"] == 5

    def test_agent_cli_visible(self, tmp_path):
        _mk_session(tmp_path, "111")
        names = [e["name"] for e in clone.list_tree(tmp_path)]
        assert ".agent-cli" in names

    def test_nested_level(self, tmp_path):
        _mk_session(tmp_path, "111")
        sub = clone.list_tree(tmp_path, ".agent-cli/sessions")
        assert [e["name"] for e in sub] == ["111"]

    def test_missing_dir_empty(self, tmp_path):
        assert clone.list_tree(tmp_path, "nope") == []


class TestClonePathsFilesOnly:
    def test_copies_selected_files_no_session(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "main.py").write_text("print(1)")
        (src / "sub").mkdir()
        (src / "sub" / "a.txt").write_text("A")
        sid = clone.clone_paths(src, dst, ["main.py", "sub"], new_session_id="999")
        assert sid is None  # 세션 없음 = fresh
        assert (dst / "main.py").read_text() == "print(1)"
        assert (dst / "sub" / "a.txt").read_text() == "A"


class TestClonePathsSessionRemap:
    def test_session_remap_full(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        _mk_session(src, "1111111111")
        (src / "code.py").write_text("x")

        sid = clone.clone_paths(
            src, dst, [".agent-cli", "code.py"], new_session_id="2222222222"
        )
        # 새 sid 반환 → 호출부가 set_session_id 로 --resume 배선
        assert sid == "2222222222"
        newdir = dst / ".agent-cli" / "sessions" / "2222222222"
        assert newdir.is_dir()
        assert not (dst / ".agent-cli" / "sessions" / "1111111111").exists()
        # 대화 레코드 보존
        assert (newdir / "history.jsonl").exists()
        assert (newdir / "agents.json").exists()
        # 사이드카 제외
        for name in ("web.json", "status.json", "instance.log"):
            assert not (newdir / name).exists(), name
        # _meta 재작성: session_id + workspace
        header = json.loads((newdir / "session.jsonl").read_text().splitlines()[0])
        assert header["_meta"]["session_id"] == "2222222222"
        assert header["_meta"]["workspace"] == str(dst.resolve())
        # _meta 아래 대화 레코드는 그대로
        second = (newdir / "session.jsonl").read_text().splitlines()[1]
        assert json.loads(second)["content"] == "hi"
        # 일반 파일도 복사됨
        assert (dst / "code.py").read_text() == "x"

    def test_selecting_only_session_dir_deep(self, tmp_path):
        """`.agent-cli/sessions/<sid>` 만 콕 집어 선택해도 remap 된다."""
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        _mk_session(src, "1111111111")
        sid = clone.clone_paths(
            src,
            dst,
            [".agent-cli/sessions/1111111111"],
            new_session_id="3333333333",
        )
        assert sid == "3333333333"
        assert (dst / ".agent-cli" / "sessions" / "3333333333").is_dir()

    def test_sidecars_excluded_even_if_dir_copy(self, tmp_path):
        """디렉토리 통째 복사여도 사이드카는 안 들어온다(ignore_patterns
        + remap 재삭제 이중 방어)."""
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        _mk_session(src, "1111111111")
        clone.clone_paths(src, dst, [".agent-cli"], new_session_id="4444444444")
        newdir = dst / ".agent-cli" / "sessions" / "4444444444"
        assert not (newdir / "web.json").exists()

    def test_no_meta_key_left_intact(self, tmp_path):
        """session.jsonl 에 _meta 가 없으면(손상) 조용히 넘어감."""
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        d = src / ".agent-cli" / "sessions" / "1111111111"
        d.mkdir(parents=True)
        (d / "session.jsonl").write_text('{"other":1}\n')
        sid = clone.clone_paths(src, dst, [".agent-cli"], new_session_id="5555555555")
        assert sid == "5555555555"  # rename 은 됨
        header = json.loads(
            (dst / ".agent-cli" / "sessions" / "5555555555" / "session.jsonl")
            .read_text()
            .splitlines()[0]
        )
        assert header == {"other": 1}  # 손 안 댐


class TestWorkspacePathRewrite:
    """clone 후 .agent-cli 내 이전 workspace 절대경로 → 새 것 치환
    (v1.22.0) — 이어받은 대화에 옛 경로 누출 방지 (사용자 보고)."""

    def test_old_workspace_path_rewritten_everywhere(self, tmp_path):
        root = tmp_path / "wsroot"
        src = root / "OLDPOSTID"
        dst = root / "NEWPOSTID"
        src.mkdir(parents=True)
        dst.mkdir(parents=True)
        sdir = src / ".agent-cli" / "sessions" / "1111111111"
        adir = sdir / "agents" / "agt-abc"
        adir.mkdir(parents=True)
        old = str(src.resolve())
        # session.jsonl _meta.workspace + history 도구 인자 + 중첩 agent history
        (sdir / "session.jsonl").write_text(
            '{"_meta": {"session_id": "1111111111", "workspace": "' + old + '"}}\n'
            '{"role": "user", "content": "read ' + old + '/main.py"}\n'
        )
        (sdir / "history.jsonl").write_text(
            '{"tool": "read_file", "path": "' + old + '/src/a.py"}\n'
        )
        (adir / "history.jsonl").write_text(
            '{"observation": "wrote ' + old + '/b.py"}\n'
        )
        # 워크스페이스 밖 절대경로 — 안 건드려야
        (sdir / "notes.md").write_text("system: /etc/hosts and " + old + "/x")

        clone.clone_paths(src, dst, [".agent-cli"], new_session_id="2222222222")
        new = str(dst.resolve())
        newdir = dst / ".agent-cli" / "sessions" / "2222222222"

        # 모든 파일에서 옛 경로 소멸 + 새 경로로
        for rel in ["session.jsonl", "history.jsonl", "agents/agt-abc/history.jsonl"]:
            body = (newdir / rel).read_text()
            assert old not in body, rel
            assert new in body, rel
        notes = (newdir / "notes.md").read_text()
        assert "/etc/hosts" in notes  # 워크스페이스 밖은 보존
        assert old not in notes and new in notes

    def test_binary_and_pathless_files_untouched(self, tmp_path):
        root = tmp_path / "wsroot"
        src, dst = root / "OLD", root / "NEW"
        src.mkdir(parents=True)
        dst.mkdir(parents=True)
        ad = src / ".agent-cli"
        ad.mkdir()
        (ad / "bin.dat").write_bytes(b"\x00\x01\xff" + str(src).encode())
        (ad / "plain.txt").write_text("no path here")
        clone.clone_paths(src, dst, [".agent-cli"], new_session_id="9")
        # 바이너리는 그대로(치환 안 함 — 크래시도 안 함), plain 도 무변경
        assert (dst / ".agent-cli" / "bin.dat").read_bytes().startswith(b"\x00\x01\xff")
        assert (dst / ".agent-cli" / "plain.txt").read_text() == "no path here"


class TestRemapSessionEdges:
    """세션 remap 엣지 (v1.21.1 감사) — 크래시/조용한 데이터 경로."""

    def test_corrupt_meta_first_line_survives(self, tmp_path):
        """session.jsonl 첫 줄이 깨진 JSON(중단된 writer) 이어도 remap 은
        rename 까지 하고 크래시하지 않는다 — 안 그러면 POST 500+롤백."""
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        d = src / ".agent-cli" / "sessions" / "1111111111"
        d.mkdir(parents=True)
        (d / "session.jsonl").write_text("{bad json\n")
        sid = clone.clone_paths(src, dst, [".agent-cli"], new_session_id="6666666666")
        assert sid == "6666666666"  # rename 은 됨
        newmeta = (
            dst / ".agent-cli" / "sessions" / "6666666666" / "session.jsonl"
        ).read_text()
        assert newmeta == "{bad json\n"  # 손 안 댐(파싱 실패=그대로)

    def test_session_dir_without_session_jsonl(self, tmp_path):
        """세션 dir 에 session.jsonl 이 없으면 rename 만 하고 재작성 스킵."""
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        d = src / ".agent-cli" / "sessions" / "1111111111"
        d.mkdir(parents=True)
        (d / "history.jsonl").write_text("{}\n")  # session.jsonl 없음
        sid = clone.clone_paths(src, dst, [".agent-cli"], new_session_id="7777777777")
        assert sid == "7777777777"
        newdir = dst / ".agent-cli" / "sessions" / "7777777777"
        assert newdir.is_dir() and (newdir / "history.jsonl").exists()

    def test_multiple_session_dirs_only_first_remapped(self, tmp_path):
        """한 post=한 세션 불변식: 여러 세션 dir 이면 정렬상 첫 것만 remap,
        나머지는 그대로(docstring 계약 고정)."""
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        _mk_session(src, "1111111111", sidecars=False)
        _mk_session(src, "2222222222", sidecars=False)
        sid = clone.clone_paths(src, dst, [".agent-cli"], new_session_id="9999999999")
        sessions = sorted(p.name for p in (dst / ".agent-cli" / "sessions").iterdir())
        # 첫(정렬) 것만 새 sid 로, 나머지는 원래 이름 유지
        assert sid == "9999999999"
        assert sessions == ["2222222222", "9999999999"]


class TestClonePathsSafety:
    def test_traversal_path_rejected(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (tmp_path / "secret").write_text("s")
        with pytest.raises(ValueError):
            clone.clone_paths(src, dst, ["../secret"], new_session_id="9")

    def test_missing_source_path_skipped(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        sid = clone.clone_paths(src, dst, ["nope.txt"], new_session_id="9")
        assert sid is None
