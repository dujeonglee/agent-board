"""Post id generation — the constraints that come from ``post_id`` being three
things at once (PK, URL path segment, workspace directory name).

Replaced ``uuid.uuid4().hex`` (32 chars). The directory name told you nothing
about which post owned it, and the absolute path rode into the agent's context
on every file operation where 32 hex characters tokenize badly.
"""

from __future__ import annotations

import re
import string

from agent_board.ids import ALPHABET, ID_LENGTH, new_post_id


class TestAlphabet:
    def test_lowercase_only(self):
        """macOS/Windows filesystems are case-insensitive: a mixed-case
        alphabet could mint two ids that are distinct PKs but the SAME
        directory."""
        assert ALPHABET == ALPHABET.lower()

    def test_no_confusable_glyphs(self):
        """A human matching a directory name against the id on a post card must
        not have to distinguish 1/l/i or 0/O."""
        for ch in "ilou":
            assert ch not in ALPHABET, ch

    def test_alphanumeric_and_unique(self):
        assert set(ALPHABET) <= set(string.ascii_lowercase + string.digits)
        assert len(set(ALPHABET)) == len(ALPHABET)


class TestGeneratedIds:
    def test_length_and_charset(self):
        for _ in range(200):
            pid = new_post_id()
            assert len(pid) == ID_LENGTH
            assert set(pid) <= set(ALPHABET)

    def test_url_path_safe(self):
        """The route is ``/s/{post_id}/{path:path}`` — a ``/`` or a character
        needing percent-encoding would break it (and percent-encoding would
        cost MORE context than the hex id it replaced)."""
        for _ in range(200):
            pid = new_post_id()
            assert re.fullmatch(r"[a-z0-9]+", pid), pid
            assert pid == pid.strip()

    def test_filesystem_safe(self):
        """It becomes a directory name under the workspaces root."""
        bad = set('/\\:*?"<>|') | {"."}
        for _ in range(200):
            pid = new_post_id()
            assert not (set(pid) & bad)
            assert pid not in (".", "..")

    def test_actually_random(self):
        assert len({new_post_id() for _ in range(500)}) > 490

    def test_shorter_than_the_uuid_it_replaced(self):
        assert ID_LENGTH < 32

    def test_uses_a_cryptographic_source(self):
        """With the default ``board-proxy`` gateway the ``/s/<id>`` route has no
        auth, so the id is the only thing guarding an instance on a LAN. A
        predictable PRNG would let one id expose the others."""
        import inspect

        from agent_board import ids

        src = inspect.getsource(ids)
        assert "secrets" in src
        assert not re.search(r"^import random$", src, re.MULTILINE)


class TestPathCost:
    def test_a_workspace_path_is_much_shorter_now(self, tmp_path):
        from agent_board.config import Config

        cfg = Config(data_dir=tmp_path, workspaces_root=tmp_path / "ws")
        new = cfg.workspace_for(new_post_id())
        old = cfg.workspaces_root.parent / "workspaces" / ("a" * 32)
        assert len(str(new)) < len(str(old)) - 30
