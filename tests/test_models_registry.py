"""agent-cli 의 서버 표·서버별 모델 표 읽기 (DESIGN §8, v1.48.0).

Integration contract (agent-cli ≥ 10.144.0): models.json ``{"endpoints": {"<server>":
{"models": {"<id>": {...}}}}}``, config.json ``{"endpoint", "endpoints": {...}}``. 옛 모양은
읽지 않는다(서버 없음).
"""

from __future__ import annotations

import json

from agent_board import models_registry as mr


def _write(path, models, server="s"):
    path.write_text(
        json.dumps({"endpoints": {server: {"models": models}}}), encoding="utf-8"
    )


def test_lists_ids_sorted(tmp_path):
    p = tmp_path / "models.json"
    _write(p, {"Zeta": {"provider": "x"}, "alpha": {"context_window": 1000}})
    out = mr.list_models(p, "s")
    assert [m["id"] for m in out] == ["alpha", "Zeta"]  # case-insensitive sort
    assert out[0]["context_window"] == 1000
    assert out[1]["provider"] == "x"
    assert out[0]["dialect"] is None and out[1]["dialect"] is None


def test_lists_dialect_binding(tmp_path):
    p = tmp_path / "models.json"
    _write(p, {"a": {"dialect": "xml_fc"}, "b": {"dialect": ""}, "c": {"dialect": 3}})
    out = {m["id"]: m["dialect"] for m in mr.list_models(p, "s")}
    assert out == {"a": "xml_fc", "b": None, "c": None}


def test_old_wire_format_key_is_not_a_binding(tmp_path):
    # agent-cli v10.4.0 과 같은 판정 — 옛 키만 있으면 미설정
    p = tmp_path / "models.json"
    _write(p, {"a": {"wire_format": "xml_fc"}})
    assert mr.list_models(p, "s")[0]["dialect"] is None
    assert mr.model_binding(p, "s", "a") is None


def test_model_binding_lookup(tmp_path):
    p = tmp_path / "models.json"
    _write(p, {"a": {"dialect": "json_fc"}, "b": {}})
    assert mr.model_binding(p, "s", "a") == "json_fc"
    assert mr.model_binding(p, "s", "b") is None
    assert mr.model_binding(p, "s", "nope") is None
    assert mr.model_binding(p, "s", None) is None
    assert mr.model_binding(p, "s", "") is None


def test_model_binding_reads_fresh_file(tmp_path):
    # 어드민 편집 직후의 열기가 새 값을 봐야 한다 — 캐시 없음
    p = tmp_path / "models.json"
    _write(p, {"a": {}})
    assert mr.model_binding(p, "s", "a") is None
    _write(p, {"a": {"dialect": "json_fc"}})
    assert mr.model_binding(p, "s", "a") == "json_fc"


def test_binding_of():
    assert mr.binding_of({"dialect": "json_fc"}) == "json_fc"
    assert mr.binding_of({"dialect": ""}) is None
    assert mr.binding_of({}) is None
    assert mr.binding_of(None) is None


def test_missing_file_is_empty(tmp_path):
    assert mr.list_models(tmp_path / "nope.json", "s") == []


def test_corrupt_file_is_empty(tmp_path):
    p = tmp_path / "models.json"
    p.write_text("{not json", encoding="utf-8")
    assert mr.list_models(p, "s") == []


def test_no_models_key_is_empty(tmp_path):
    p = tmp_path / "models.json"
    p.write_text(json.dumps({"endpoints": {"s": {}}}), encoding="utf-8")
    assert mr.list_models(p, "s") == []


def test_legacy_shape_is_not_read(tmp_path):
    """agent-cli 와 같이 옛 모양은 읽지 않는다 — 같은 이름의 모델이 있어도 비어 있다."""
    p = tmp_path / "models.json"
    p.write_text(
        json.dumps({"models": {"a": {"dialect": "json_fc"}}}), encoding="utf-8"
    )
    assert mr.list_models(p, "s") == [] and mr.model_binding(p, "s", "a") is None


def test_models_are_per_server(tmp_path):
    p = tmp_path / "models.json"
    p.write_text(
        json.dumps(
            {
                "endpoints": {
                    "s": {"models": {"a": {"dialect": "json_fc"}}},
                    "t": {"models": {"a": {"dialect": "xml_fc"}, "b": {}}},
                }
            }
        ),
        encoding="utf-8",
    )
    assert mr.model_binding(p, "s", "a") == "json_fc"
    assert mr.model_binding(p, "t", "a") == "xml_fc"
    assert [m["id"] for m in mr.list_models(p, "t")] == ["a", "b"]
    assert mr.list_models(p, "nope") == [] and mr.list_models(p, None) == []


def test_server_names_and_default(tmp_path):
    c = tmp_path / "config.json"
    c.write_text(
        json.dumps({"endpoint": "b", "endpoints": {"b": {}, "a": {}, "bad": 3}}),
        encoding="utf-8",
    )
    assert mr.server_names(c) == (["a", "b"], "b")
    c.write_text(json.dumps({"endpoint": "gone", "endpoints": {"a": {}}}))
    assert mr.server_names(c) == (["a"], None)  # 저장된 이름이 아닌 기본은 없다
    c.write_text(json.dumps({"provider": "openai", "base_url": "http://x/v1"}))
    assert mr.server_names(c) == ([], None)  # 옛 모양 = 서버 없음
    assert mr.server_names(tmp_path / "none.json") == ([], None)


def test_list_servers_shows_host_only(tmp_path):
    """새 글 폼의 서버 목록 — 호스트만, 경로·쿼리·사용자 정보·키·헤더 값은 없다."""
    pytest = __import__("pytest")
    pytest.importorskip("agent_cli")
    c = tmp_path / "config.json"
    secret = "SENTINEL-query-token-31"
    c.write_text(
        json.dumps(
            {
                "endpoint": "work",
                "endpoints": {
                    "work": {
                        "base_url": f"https://u:pw@gw.example.com:8443/v1?token={secret}",
                        "api_key": "SENTINEL-key-31",
                        "default_model": "m",
                        "extra_headers": [{"name": "X-A", "value": "SENTINEL-hdr-31"}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    out = mr.list_servers(c)
    assert out == {
        "default": "work",
        "servers": [
            {
                "name": "work",
                "host": "gw.example.com:8443",
                "default": True,
                "default_model": "m",
            }
        ],
    }
    assert "SENTINEL" not in json.dumps(out) and "pw" not in json.dumps(out)
