"""Workspace path shape — the two things that decide how much of an agent's
context a file path eats (v1.30.0).

A workspace path is not incidental: it appears in every ``read_file`` /
``write_file`` / ``shell`` argument, an ``action_input`` is re-fed to the model
on every subsequent turn, and paths accumulate in the compaction file list. So
both halves of ``<workspaces_root>/<post_id>`` are context cost.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from agent_board.config import Config
from agent_board.ids import new_post_id


class TestDefaultRoot:
    def test_default_directory_is_short(self):
        with patch.dict(os.environ, {"AGENT_BOARD_HOME": "/tmp/bh"}, clear=True):
            cfg = Config.from_env()
        assert cfg.workspaces_root.name == "ws"

    def test_env_override_still_wins(self, tmp_path):
        with patch.dict(
            os.environ,
            {"AGENT_BOARD_HOME": "/tmp/bh", "AGENT_BOARD_WORKSPACES": str(tmp_path)},
            clear=True,
        ):
            cfg = Config.from_env()
        assert cfg.workspaces_root == tmp_path.resolve()


class TestWorkspaceForStillDerived:
    def test_derived_never_stored(self, tmp_path):
        """The invariant that keeps the board free of a user-supplied path:
        the workspace is a pure function of post_id, so there is no injection
        surface and no shared-workspace collision. Short ids must not have
        traded it away for a stored mapping."""
        cfg = Config(data_dir=tmp_path, workspaces_root=tmp_path / "ws")
        pid = new_post_id()
        assert cfg.workspace_for(pid) == cfg.workspaces_root / pid
        assert cfg.workspaces_root in cfg.workspace_for(pid).parents

    def test_a_post_id_cannot_escape_the_root(self, tmp_path):
        """Generated ids are ``[a-z0-9]+`` so this cannot happen today — the
        test pins the property, since ``workspace_for`` is what the delete path
        trusts before ``rmtree``."""
        cfg = Config(data_dir=tmp_path, workspaces_root=tmp_path / "ws")
        for _ in range(100):
            ws = cfg.workspace_for(new_post_id()).resolve()
            assert cfg.workspaces_root.resolve() in ws.parents


class TestPathCostVsBefore:
    def test_full_path_is_substantially_shorter(self):
        base = Path("/Users/someone/work/harness/data")
        before = base / "workspaces" / ("cf2dad8ec6894e6583a72b0ba02fe98e")
        after = base / "ws" / new_post_id()
        saved = len(str(before)) - len(str(after))
        assert saved >= 30, (str(before), str(after), saved)
