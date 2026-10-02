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
    assert out[0]["dialect"] is None and out[1]["dialect"] is None


def test_lists_dialect_binding(tmp_path):
    p = tmp_path / "models.json"
    _write(p, {"a": {"dialect": "xml_fc"}, "b": {"dialect": ""}, "c": {"dialect": 3}})
    out = {m["id"]: m["dialect"] for m in mr.list_models(p)}
    assert out == {"a": "xml_fc", "b": None, "c": None}


def test_old_wire_format_key_is_not_a_binding(tmp_path):
    # agent-cli v10.4.0 과 같은 판정 — 옛 키만 있으면 미설정
    p = tmp_path / "models.json"
    _write(p, {"a": {"wire_format": "xml_fc"}})
    assert mr.list_models(p)[0]["dialect"] is None
    assert mr.model_binding(p, "a") is None


def test_model_binding_lookup(tmp_path):
    p = tmp_path / "models.json"
    _write(p, {"a": {"dialect": "json_fc"}, "b": {}})
    assert mr.model_binding(p, "a") == "json_fc"
    assert mr.model_binding(p, "b") is None
    assert mr.model_binding(p, "nope") is None
    assert mr.model_binding(p, None) is None
    assert mr.model_binding(p, "") is None


def test_model_binding_reads_fresh_file(tmp_path):
    # 어드민 편집 직후의 열기가 새 값을 봐야 한다 — 캐시 없음
    p = tmp_path / "models.json"
    _write(p, {"a": {}})
    assert mr.model_binding(p, "a") is None
    _write(p, {"a": {"dialect": "json_fc"}})
    assert mr.model_binding(p, "a") == "json_fc"


def test_binding_of():
    assert mr.binding_of({"dialect": "json_fc"}) == "json_fc"
    assert mr.binding_of({"dialect": ""}) is None
    assert mr.binding_of({}) is None
    assert mr.binding_of(None) is None


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
