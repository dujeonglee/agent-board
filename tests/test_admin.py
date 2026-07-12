"""Admin 페이지 — agent-cli config.json / models.json 편집 (도메인 + HTTP).

프로브(httpx)·capability 탐지(agent_cli)는 전부 monkeypatch — 네트워크/
LLM 없이 분류·마스킹·보존 계약을 고정한다.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_board import admin
from agent_board.app import create_app
from agent_board.config import Config
from agent_board.store import Store


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _cfg_file(tmp_path, **over):
    p = tmp_path / "config.json"
    data = {
        "provider": "openai",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": "sk-secret",
        "default_model": "m1",
        "custom_knob": 42,  # 폼 밖 키 — 보존 계약 검증용
    }
    data.update(over)
    _write_json(p, data)
    return p


def _models_file(tmp_path, models=None):
    p = tmp_path / "models.json"
    _write_json(
        p,
        {
            "models": models
            if models is not None
            else {
                "m1": {"context_window": 1000},
                "gone": {"context_window": 2000},
            },
            "provider_defaults": {"keep": True},
        },
    )
    return p


def _fake_served(monkeypatch, ids):
    """httpx.get 을 OpenAI /models 응답으로 대체."""

    def fake_get(url, headers=None, timeout=None):
        req = httpx.Request("GET", url)
        return httpx.Response(200, json={"data": [{"id": i} for i in ids]}, request=req)

    monkeypatch.setattr(admin.httpx, "get", fake_get)


# ── config.json 도메인 ────────────────────────────────────────────


class TestAdminConfig:
    def test_get_masks_api_key(self, tmp_path):
        p = _cfg_file(tmp_path)
        c = admin.get_config(p)
        assert c["api_key"] == "***"
        assert c["base_url"] == "http://127.0.0.1:8000/v1"
        assert c["exists"] is True

    def test_get_missing_file_is_empty_form(self, tmp_path):
        c = admin.get_config(tmp_path / "none.json")
        assert c["exists"] is False and c["api_key"] == ""

    def test_put_mask_sentinel_keeps_existing_key(self, tmp_path):
        p = _cfg_file(tmp_path)
        admin.update_config({"api_key": "***", "base_url": "http://new:1/v1"}, p)
        saved = json.loads(p.read_text())
        assert saved["api_key"] == "sk-secret"  # 유지
        assert saved["base_url"] == "http://new:1/v1"

    def test_put_empty_key_keeps_existing_key(self, tmp_path):
        p = _cfg_file(tmp_path)
        admin.update_config({"api_key": ""}, p)
        assert json.loads(p.read_text())["api_key"] == "sk-secret"

    def test_put_new_key_replaces(self, tmp_path):
        p = _cfg_file(tmp_path)
        admin.update_config({"api_key": "sk-new"}, p)
        assert json.loads(p.read_text())["api_key"] == "sk-new"

    def test_put_preserves_unknown_keys(self, tmp_path):
        p = _cfg_file(tmp_path)
        admin.update_config({"default_model": "m2"}, p)
        saved = json.loads(p.read_text())
        assert saved["custom_knob"] == 42
        assert saved["default_model"] == "m2"

    def test_put_creates_file_when_absent(self, tmp_path):
        p = tmp_path / "sub" / "config.json"
        admin.update_config({"provider": "openai", "base_url": "http://x/v1"}, p)
        assert json.loads(p.read_text())["base_url"] == "http://x/v1"

    def test_put_rejects_non_string(self, tmp_path):
        p = _cfg_file(tmp_path)
        with pytest.raises(admin.AdminError):
            admin.update_config({"base_url": 123}, p)


# ── 서빙 프로브 + 분류 ────────────────────────────────────────────


class TestModelStatus:
    def test_served_missing_new_classification(self, tmp_path, monkeypatch):
        cfg = _cfg_file(tmp_path)
        models = _models_file(tmp_path)  # registry: m1, gone
        _fake_served(monkeypatch, ["m1", "fresh"])  # server: m1, fresh
        view = admin.list_models_with_status(models, cfg)
        by_id = {r["id"]: r["status"] for r in view["models"]}
        assert by_id == {"m1": "served", "gone": "missing"}
        assert view["new"] == ["fresh"]
        assert view["probe_error"] == ""

    def test_probe_failure_degrades_to_unknown(self, tmp_path, monkeypatch):
        cfg = _cfg_file(tmp_path)
        models = _models_file(tmp_path)

        def boom(url, headers=None, timeout=None):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(admin.httpx, "get", boom)
        view = admin.list_models_with_status(models, cfg)
        assert view["probe_error"]
        assert {r["status"] for r in view["models"]} == {"unknown"}
        assert view["new"] == []  # 프로브 없인 NEW 판단 불가

    def test_anthropic_uses_v1_models_and_key_header(self, tmp_path, monkeypatch):
        cfg = _cfg_file(
            tmp_path, provider="anthropic", base_url="https://api.anthropic.com"
        )
        seen = {}

        def fake_get(url, headers=None, timeout=None):
            seen["url"], seen["headers"] = url, headers
            req = httpx.Request("GET", url)
            return httpx.Response(200, json={"data": [{"id": "c1"}]}, request=req)

        monkeypatch.setattr(admin.httpx, "get", fake_get)
        assert admin.list_served_models(cfg) == ["c1"]
        assert seen["url"].endswith("/v1/models")
        assert seen["headers"]["x-api-key"] == "sk-secret"


# ── models.json 편집 ─────────────────────────────────────────────


class TestModelEntryEdit:
    def test_save_preserves_other_models_and_top_keys(self, tmp_path):
        models = _models_file(tmp_path)
        admin.save_model_entry("m1", {"context_window": 9999}, models)
        saved = json.loads(models.read_text())
        assert saved["models"]["m1"] == {"context_window": 9999}
        assert saved["models"]["gone"]["context_window"] == 2000
        assert saved["provider_defaults"] == {"keep": True}

    def test_save_new_model(self, tmp_path):
        models = _models_file(tmp_path)
        admin.save_model_entry("fresh", {"context_window": 8192}, models)
        assert "fresh" in json.loads(models.read_text())["models"]

    def test_delete_removes_and_reports(self, tmp_path):
        models = _models_file(tmp_path)
        assert admin.delete_model_entry("gone", models) is True
        assert admin.delete_model_entry("gone", models) is False
        assert "m1" in json.loads(models.read_text())["models"]


# ── capability 탐지 (agent_cli 재사용) ───────────────────────────


class TestDetect:
    def test_detect_returns_entry_without_saving(self, tmp_path, monkeypatch):
        cfg = _cfg_file(tmp_path)
        import agent_cli.providers.capabilities as caps_mod

        fake_caps = caps_mod.ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        seen = {}

        def fake_detect(provider, base_url, model, api_key=""):
            seen.update(provider=provider, base_url=base_url, model=model)
            return fake_caps

        monkeypatch.setattr(caps_mod, "_detect_runtime_capabilities", fake_detect)
        entry = admin.detect_model_entry("fresh", cfg)
        assert entry["context_window"] == 32768
        assert entry["_auto_detected"] is True
        assert seen["model"] == "fresh" and seen["provider"] == "openai"

    def test_detect_failure_is_admin_error(self, tmp_path, monkeypatch):
        cfg = _cfg_file(tmp_path)
        import agent_cli.providers.capabilities as caps_mod

        monkeypatch.setattr(
            caps_mod, "_detect_runtime_capabilities", lambda *a, **k: None
        )
        with pytest.raises(admin.AdminError):
            admin.detect_model_entry("fresh", cfg)


# ── HTTP 라우트 ──────────────────────────────────────────────────


class _NoopOrch:
    async def open(self, post_id):
        return f"/s/{post_id}/"


def _admin_client(tmp_path):
    cfg_json = _cfg_file(tmp_path)
    models_json = _models_file(tmp_path)
    cfg = Config(
        data_dir=tmp_path / "data",
        workspaces_root=tmp_path / "ws",
        models_json=models_json,
        agent_cli_config_json=cfg_json,
    )
    app = create_app(
        cfg, store=Store(cfg.db_path), orchestrator=_NoopOrch(), keepalive=object()
    )
    return cfg_json, models_json, TestClient(app)


class TestAdminApi:
    def test_admin_page_served(self, tmp_path):
        _, _, c = _admin_client(tmp_path)
        r = c.get("/admin")
        assert r.status_code == 200 and "admin" in r.text.lower()

    def test_get_config_masked(self, tmp_path):
        _, _, c = _admin_client(tmp_path)
        body = c.get("/api/admin/config").json()
        assert body["api_key"] == "***"
        assert "sk-secret" not in json.dumps(body)

    def test_put_config_roundtrip(self, tmp_path):
        cfg_json, _, c = _admin_client(tmp_path)
        r = c.put("/api/admin/config", json={"default_model": "m9", "api_key": "***"})
        assert r.status_code == 200
        saved = json.loads(cfg_json.read_text())
        assert saved["default_model"] == "m9" and saved["api_key"] == "sk-secret"

    def test_models_listing_with_probe(self, tmp_path, monkeypatch):
        _, _, c = _admin_client(tmp_path)
        _fake_served(monkeypatch, ["m1", "fresh"])
        body = c.get("/api/admin/models").json()
        assert body["new"] == ["fresh"]
        assert {r["id"]: r["status"] for r in body["models"]} == {
            "m1": "served",
            "gone": "missing",
        }

    def test_put_and_delete_model(self, tmp_path):
        _, models_json, c = _admin_client(tmp_path)
        r = c.put("/api/admin/models/fresh", json={"context_window": 4096})
        assert r.status_code == 200
        assert "fresh" in json.loads(models_json.read_text())["models"]
        assert c.delete("/api/admin/models/fresh").status_code == 200
        assert c.delete("/api/admin/models/fresh").status_code == 404

    def test_detect_endpoint_requires_model(self, tmp_path):
        _, _, c = _admin_client(tmp_path)
        assert c.post("/api/admin/models/detect", json={}).status_code == 400

    def test_index_has_admin_link(self, tmp_path):
        _, _, c = _admin_client(tmp_path)
        assert 'href="/admin"' in c.get("/").text


class TestNoCacheHeaders:
    """정적/페이지 응답의 no-cache — 코드 교체 후 옛 UI 가 캐시로 남아
    admin 링크가 안 보이던 실사례(v1.11.1) 회귀 가드."""

    def test_index_and_admin_no_cache(self, tmp_path):
        _, _, c = _admin_client(tmp_path)
        for path in ("/", "/admin"):
            r = c.get(path)
            assert r.headers.get("cache-control") == "no-cache, must-revalidate", path

    def test_static_no_cache(self, tmp_path):
        _, _, c = _admin_client(tmp_path)
        r = c.get("/static/app.js")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-cache, must-revalidate"
