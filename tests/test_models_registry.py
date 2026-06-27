"""Read agent-cli's models.json registry to list selectable models (DESIGN §8).

Integration contract: ``{"models": {"<id>": {provider?, context_window?, ...}}}``
(agent-cli ~/.agent-cli/models.json). The board only needs the ids (+ provider /
context_window for display).
"""

from __future__ import annotations

import json

from agent_board import models_registry as mr


def _write(path, models):
    path.write_text(json.dumps({"models": models}), encoding="utf-8")


def test_lists_ids_sorted(tmp_path):
    p = tmp_path / "models.json"
    _write(p, {"Zeta": {"provider": "x"}, "alpha": {"context_window": 1000}})
    out = mr.list_models(p)
    assert [m["id"] for m in out] == ["alpha", "Zeta"]  # case-insensitive sort
    assert out[0]["context_window"] == 1000
    assert out[1]["provider"] == "x"


def test_missing_file_is_empty(tmp_path):
    assert mr.list_models(tmp_path / "nope.json") == []


def test_corrupt_file_is_empty(tmp_path):
    p = tmp_path / "models.json"
    p.write_text("{not json", encoding="utf-8")
    assert mr.list_models(p) == []


def test_no_models_key_is_empty(tmp_path):
    p = tmp_path / "models.json"
    p.write_text(json.dumps({"provider_defaults": {}}), encoding="utf-8")
    assert mr.list_models(p) == []
