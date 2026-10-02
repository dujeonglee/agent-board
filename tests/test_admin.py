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
        admin.save_model_entry(
            "m1", {"context_window": 9999, "dialect": "json_fc"}, models
        )
        saved = json.loads(models.read_text())
        assert saved["models"]["m1"] == {"context_window": 9999, "dialect": "json_fc"}
        assert saved["models"]["gone"]["context_window"] == 2000
        assert saved["provider_defaults"] == {"keep": True}

    def test_save_new_model(self, tmp_path):
        models = _models_file(tmp_path)
        admin.save_model_entry(
            "fresh", {"context_window": 8192, "dialect": "xml_fc"}, models
        )
        assert "fresh" in json.loads(models.read_text())["models"]

    def test_save_without_dialect_is_refused(self, tmp_path):
        # v1.33.0: 바인딩 필수 — 없으면 agent-cli 가 실행하지 않는 모델이 된다
        models = _models_file(tmp_path)
        for entry in ({"context_window": 1}, {"dialect": ""}, {"dialect": None}):
            with pytest.raises(admin.AdminError, match="dialect"):
                admin.save_model_entry("m1", entry, models)
        assert json.loads(models.read_text())["models"]["m1"] == {
            "context_window": 1000
        }  # 그대로

    def test_save_with_unknown_dialect_is_refused(self, tmp_path, monkeypatch):
        models = _models_file(tmp_path)
        monkeypatch.setattr(admin, "list_dialect_names", lambda: ["json_fc"])
        with pytest.raises(admin.AdminError, match="알 수 없는 dialect 'nope'"):
            admin.save_model_entry("m1", {"dialect": "nope"}, models)

    def test_save_skips_name_check_when_agent_cli_missing(self, tmp_path, monkeypatch):
        models = _models_file(tmp_path)
        monkeypatch.setattr(admin, "list_dialect_names", list)
        admin.save_model_entry("m1", {"dialect": "whatever"}, models)
        assert json.loads(models.read_text())["models"]["m1"]["dialect"] == "whatever"

    def test_old_wire_format_key_does_not_count_as_binding(self, tmp_path):
        models = _models_file(tmp_path)
        with pytest.raises(admin.AdminError, match="dialect"):
            admin.save_model_entry("m1", {"wire_format": "xml_fc"}, models)

    def test_delete_removes_and_reports(self, tmp_path):
        models = _models_file(tmp_path)
        assert admin.delete_model_entry("gone", models) is True
        assert admin.delete_model_entry("gone", models) is False
        assert "m1" in json.loads(models.read_text())["models"]


# ── capability 탐지 (agent_cli 재사용) ───────────────────────────


class TestDetect:
    @pytest.fixture(autouse=True)
    def _require_agent_cli(self):
        # These validate the REAL agent-cli capability detector — a co-install
        # in dev/CI. Skip (not fail) where agent-cli is absent.
        pytest.importorskip("agent_cli")

    def test_detect_returns_entry_without_saving(self, tmp_path, monkeypatch):
        cfg = _cfg_file(tmp_path)
        import agent_cli.providers.capabilities as caps_mod

        fake_caps = caps_mod.ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
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
        r = c.put(
            "/api/admin/models/fresh",
            json={"context_window": 4096, "dialect": "json_fc"},
        )
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


class TestThemeAndButtonSystem:
    """agent-cli 와 공유하는 디자인 계약 (v1.12.0) — 5테마 토큰·버튼 4변형·
    UA-기본 차단 베이스·localStorage 키 공유."""

    def test_css_has_five_themes_and_variants(self, tmp_path):
        _, _, c = _admin_client(tmp_path)
        css = c.get("/static/style.css").text
        for theme in ("midnight", "terminal", "amber", "light"):
            assert f':root[data-theme="{theme}"]' in css, theme
        for cls in (".btn-primary", ".btn-ghost", ".btn-danger", ".btn-icon"):
            assert cls in css, cls
        import re

        m = re.search(r"\nbutton \{[^}]*\}", css)
        assert m and "background: transparent" in m.group(0)

    def test_pages_share_theme_storage_key(self, tmp_path):
        _, _, c = _admin_client(tmp_path)
        for path in ("/", "/admin"):
            html = c.get(path).text
            assert "agentcli_theme" in html, path  # FOUC 방지 초기화 스크립트

    def test_index_has_theme_picker(self, tmp_path):
        _, _, c = _admin_client(tmp_path)
        html = c.get("/").text
        assert 'id="theme-btn"' in html and 'id="theme-menu"' in html
        js = c.get("/static/app.js").text
        assert "agentcli_theme" in js and "theme-item" in js


class TestDialectBinding:
    """바인딩 UX ① (agent-cli dialects): 모델 entry 의 dialect
    바인딩을 admin 에서 드롭다운으로 편집(v1.32.0, agent-cli v10.0.0 짝). auto=필드 미기록(keep-sentinel
    동형), 목록은 agent-cli lazy import — 자유입력 금지 (agent-cli 부트가
    unknown 이름 fail-fast)."""

    @pytest.fixture(autouse=True)
    def _require_agent_cli(self):
        pytest.importorskip("agent_cli")

    def test_list_dialect_names_from_agent_cli(self):
        # dev/배포 환경은 agent-cli co-install 전제 (detect 동형)
        names = admin.list_dialect_names()
        assert "json_fc" in names
        assert "xml_fc" in names
        assert "md_array" not in names  # v6.0.0 리네임
        assert "react" not in names  # v7.0.0 제거

    def test_list_dialect_names_missing_agent_cli(self, monkeypatch):
        import sys

        monkeypatch.setitem(sys.modules, "agent_cli.dialects", None)
        assert admin.list_dialect_names() == []

    def test_no_fallback_to_old_package(self, monkeypatch):
        # agent-cli v10.4.0 이 wire_formats shim 을 지웠다 — 보드도 보지 않는다
        import sys
        import types

        legacy = types.ModuleType("agent_cli.wire_formats")
        legacy.list_names = lambda: ["legacy_only"]
        monkeypatch.setitem(sys.modules, "agent_cli.dialects", None)
        monkeypatch.setitem(sys.modules, "agent_cli.wire_formats", legacy)
        assert admin.list_dialect_names() == []

    def test_models_view_includes_dialects(self, tmp_path, monkeypatch):
        _, _, c = _admin_client(tmp_path)
        r = c.get("/api/admin/models")
        assert r.status_code == 200
        body = r.json()
        assert "dialects" in body
        assert "xml_fc" in body["dialects"]
        assert "wire_formats" not in body

    def test_put_entry_with_binding_round_trips(self, tmp_path):
        _, models_json, c = _admin_client(tmp_path)
        entry = {"context_window": 8192, "dialect": "xml_fc"}
        r = c.put("/api/admin/models/qwen-x", json=entry)
        assert r.status_code == 200
        import json as _json

        saved = _json.loads(models_json.read_text())
        assert saved["models"]["qwen-x"]["dialect"] == "xml_fc"

    def test_static_wiring_dropdown(self, tmp_path):
        # 정적 배선 계약 (agent-cli test_web_server 동형): 셀렉트 id·옵션
        # 채움·필수 저장 로직·가이드·딥링크가 프론트에 실재하는지 고정.
        _, _, c = _admin_client(tmp_path)
        html = c.get("/admin").text
        assert 'id="ef-wire"' in html
        assert 'id="ef-wire-guide"' in html  # 방언 선택 가이드 한 줄
        assert "<th>dialect</th>" in html
        assert "badge unbound" in html  # 범례의 ⚠ 미설정
        js = c.get("/static/admin.js").text
        assert "modelsView.dialects" in js  # 옵션 소스
        assert "entry.dialect = dialect" in js  # 저장 시 새 키
        assert "wire_format" not in js  # 옛 키는 읽지도 쓰지도 않는다
        assert "auto (기본 체인)" not in js  # auto 선택지 없음 — 바인딩 필수
        assert 'auto.value = ""' not in js
        assert "방언을 고르세요" in js  # 빈 값은 프론트에서도 멈춘다
        assert "DIALECT_GUIDE" in js and "json_fc:" in js and "native_fc:" in js
        assert "#model=" in js  # /admin#model=<id> 딥링크

    def test_put_entry_without_dialect_is_400(self, tmp_path):
        _, models_json, c = _admin_client(tmp_path)
        r = c.put("/api/admin/models/qwen-x", json={"context_window": 8192})
        assert r.status_code == 400
        assert "dialect" in r.json()["detail"]
        import json as _json

        assert "qwen-x" not in _json.loads(models_json.read_text())["models"]

    def test_models_view_rows_carry_dialect(self, tmp_path, monkeypatch):
        monkeypatch.setattr(admin, "list_served_models", lambda cfg: ["m1"])
        _, _, c = _admin_client(tmp_path)
        c.put("/api/admin/models/m1", json={"context_window": 1, "dialect": "xml_fc"})
        rows = {
            r["id"]: r["dialect"] for r in c.get("/api/admin/models").json()["models"]
        }
        assert rows == {"m1": "xml_fc", "gone": None}


class TestBaseUrlIsRemote:
    """AUDIT B-2 defense-in-depth: recognise when a models probe would ship the
    stored API key off-box (attacker-repointed base_url exfil surface)."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8000",
            "http://127.0.0.1:8000/v1",
            "http://[::1]:1234",
            "http://192.168.1.50:8000",
            "http://10.0.0.5",
            "http://172.16.3.4:9000",
        ],
    )
    def test_local_or_private_not_remote(self, url):
        assert admin.base_url_is_remote(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            "http://evil.example.com/v1",
            "https://api.openai.com",
            "http://8.8.8.8:8000",
        ],
    )
    def test_remote_detected(self, url):
        assert admin.base_url_is_remote(url) is True

    def test_empty_is_not_remote(self):
        assert admin.base_url_is_remote("") is False

    def test_probe_warns_on_remote_key_send(self, tmp_path, monkeypatch, capsys):
        # A remote base_url with a key → stderr warning that the key leaves box.
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "provider": "openai",
                    "base_url": "http://evil.example.com",
                    "api_key": "sk-secret",
                }
            )
        )

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"id": "m1"}]}

        monkeypatch.setattr(admin.httpx, "get", lambda *a, **k: _Resp())
        admin.list_served_models(cfg)
        assert "원격 호스트" in capsys.readouterr().err

    def test_probe_no_warn_on_local(self, tmp_path, monkeypatch, capsys):
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps(
                {
                    "provider": "openai",
                    "base_url": "http://127.0.0.1:8000",
                    "api_key": "sk-secret",
                }
            )
        )

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"id": "m1"}]}

        monkeypatch.setattr(admin.httpx, "get", lambda *a, **k: _Resp())
        admin.list_served_models(cfg)
        assert "원격 호스트" not in capsys.readouterr().err


class TestSupportsGrammarField:
    """v1.31.0: models.json 의 `supports_grammar` 는 3값(없음/true/false) — 편집
    폼은 auto 를 '미기록' 으로 저장해 인스턴스의 프로브에 맡긴다."""

    def test_entry_round_trips_true_false_and_absent(self, tmp_path):
        mp = tmp_path / "models.json"
        admin.save_model_entry(
            "m",
            {
                "context_window": 1,
                "max_output_tokens": 1,
                "supports_thinking": False,
                "supports_grammar": True,
                "dialect": "json_fc",
            },
            mp,
        )
        assert admin._read_json(mp)["models"]["m"]["supports_grammar"] is True
        admin.save_model_entry(
            "m",
            {
                "context_window": 1,
                "max_output_tokens": 1,
                "supports_thinking": False,
                "dialect": "json_fc",
            },
            mp,
        )
        assert "supports_grammar" not in admin._read_json(mp)["models"]["m"]

    def test_admin_ui_wiring(self):
        from pathlib import Path

        static = Path(admin.__file__).parent / "static"
        html = (static / "admin.html").read_text(encoding="utf-8")
        js = (static / "admin.js").read_text(encoding="utf-8")
        assert 'id="ef-grammar"' in html and "<th>문법</th>" in html
        assert (
            "supports_grammar" in js and 'entry.supports_grammar = sg === "true"' in js
        )
