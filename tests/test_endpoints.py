"""모델 서버 여러 개 (v1.48.0, agent-cli ≥ 10.144.0 짝) — admin 의 서버 표·서버별 모델 표,
새 글의 서버·모델, 방의 서버 기록·거울·복제·게이트, 그리고 agent-cli 와의 계약.

키·헤더 값은 어떤 응답에도 평문으로 나가지 않는다(SENTINEL 문자열로 확인). 네트워크 없음 —
모델 목록 프로브(httpx)는 monkeypatch.
"""

from __future__ import annotations

import json
import re
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_board import admin, instances, models_registry
from agent_board.app import create_app
from agent_board.config import Config
from agent_board.models import Post
from agent_board.store import Store

_KEY = "SENTINEL-board-key-4410"
_HDR = "SENTINEL-board-header-4411"
_SECRET_QUERY = "SENTINEL-board-query-4412"


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _paths(tmp_path):
    return tmp_path / "config.json", tmp_path / "models.json"


def _no_probe(monkeypatch, ids=()):
    def fake_get(url, headers=None, timeout=None):
        return httpx.Response(
            200,
            json={"data": [{"id": i} for i in ids]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(admin.httpx, "get", fake_get)


class _Orch:
    def __init__(self):
        self.opened = []

    async def open(self, post_id):
        self.opened.append(post_id)
        return f"/s/{post_id}/"

    async def restart(self, post_id):
        return f"/s/{post_id}/"


class _Keep:
    async def enable(self, post_id):
        pass

    async def disable(self, post_id):
        pass


def _client(tmp_path):
    cfg_json, models_json = _paths(tmp_path)
    cfg = Config(
        data_dir=tmp_path / "data",
        workspaces_root=tmp_path / "ws",
        models_json=models_json,
        agent_cli_config_json=cfg_json,
    )
    store = Store(cfg.db_path)
    orch = _Orch()
    app = create_app(cfg, store=store, orchestrator=orch, keepalive=_Keep())
    return cfg, store, orch, TestClient(app)


def _server(c, name, **fields):
    body = {
        "name": name,
        "provider": "openai",
        "base_url": f"http://127.0.0.1:1/{name}/v1",
        **fields,
    }
    r = c.post("/api/admin/endpoints", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _model(c, server, mid, dialect="json_fc"):
    r = c.put(
        f"/api/admin/endpoints/{server}/models/{mid}",
        json={"context_window": 8192, "dialect": dialect},
    )
    assert r.status_code == 200, r.text


# ── 도메인: 서버 표 ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _need_agent_cli():
    pytest.importorskip("agent_cli.config")


class TestServerTable:
    def test_first_server_becomes_the_default(self, tmp_path):
        cfg, models = _paths(tmp_path)
        out = admin.create_server("home", {"base_url": "http://h/v1"}, cfg, models)
        assert out == {"name": "home", "moved": []}
        assert json.loads(cfg.read_text()) == {
            "endpoints": {"home": {"base_url": "http://h/v1"}},
            "endpoint": "home",
        }
        admin.create_server("work", {"base_url": "http://w/v1"}, cfg, models)
        assert json.loads(cfg.read_text())["endpoint"] == "home"

    def test_create_repairs_a_missing_default(self, tmp_path):
        cfg, models = _paths(tmp_path)
        _write(cfg, {"endpoints": {"a": {}}})  # 손으로 고친 파일 — 기본 없음
        assert admin.get_servers(cfg)["default_missing"] is True
        admin.create_server("b", {}, cfg, models)
        assert json.loads(cfg.read_text())["endpoint"] == "b"

    @pytest.mark.parametrize("bad", ["Home", "a b", "", "a_b", None])
    def test_name_rule_and_file_unchanged(self, tmp_path, bad):
        cfg, models = _paths(tmp_path)
        with pytest.raises(admin.AdminError):
            admin.create_server(bad, {}, cfg, models)
        assert not cfg.exists()

    def test_duplicate_is_conflict(self, tmp_path):
        cfg, models = _paths(tmp_path)
        admin.create_server("home", {}, cfg, models)
        with pytest.raises(admin.AdminConflict) as ei:
            admin.create_server("home", {}, cfg, models)
        assert ei.value.detail["reason"] == "exists"

    def test_legacy_files_are_kept_as_v1_with_the_cli_rule(
        self, tmp_path, agent_cli_config
    ):
        cfg, models = _paths(tmp_path)
        _write(cfg, {"provider": "openai", "api_key": _KEY})
        _write(models, {"models": {"m": {}}})
        (tmp_path / "config.json.v1").write_text("older")
        assert admin.get_servers(cfg, models)["legacy"] is True
        # 같은 규칙: agent-cli 가 고를 이름 그대로
        expected = [
            agent_cli_config.legacy_backup_path(cfg),
            agent_cli_config.legacy_backup_path(models),
        ]
        out = admin.create_server("home", {"base_url": "http://h/v1"}, cfg, models)
        assert out["moved"] == [str(p) for p in expected]
        assert [p.name for p in expected] == ["config.json.v1-2", "models.json.v1"]
        assert (tmp_path / "config.json.v1").read_text() == "older"
        assert (
            json.loads((tmp_path / "config.json.v1-2").read_text())["api_key"] == _KEY
        )
        assert list(json.loads(cfg.read_text())["endpoints"]) == ["home"]
        assert not models.exists()

    def test_invalid_fields_leave_legacy_files_in_place(self, tmp_path):
        cfg, models = _paths(tmp_path)
        _write(cfg, {"provider": "openai"})
        with pytest.raises(admin.AdminError):
            admin.create_server("home", {"base_url": 3}, cfg, models)
        assert json.loads(cfg.read_text()) == {"provider": "openai"}
        assert not (tmp_path / "config.json.v1").exists()

    def test_writes_refuse_a_legacy_file(self, tmp_path):
        cfg, _ = _paths(tmp_path)
        _write(cfg, {"provider": "openai"})
        with pytest.raises(admin.AdminError, match="pre-10.144.0"):
            admin.update_server("home", {}, cfg)
        with pytest.raises(admin.AdminError, match="pre-10.144.0"):
            admin.set_default_server("home", cfg)

    def test_unreadable_file_is_reported(self, tmp_path):
        cfg, models = _paths(tmp_path)
        cfg.write_text("{oops")
        view = admin.get_servers(cfg, models)
        assert view["servers"] == [] and "config.json" in view["unreadable"]


class TestUpdateKeepsWhatItDidNotTouch:
    """저장 요청 시점에 파일을 다시 읽고 받은 키만 고친다 — 폼을 연 뒤 방이 저장한 값을 지키는 법."""

    def test_a_room_saved_default_model_survives_an_admin_save(
        self, tmp_path, agent_cli_config
    ):
        cfg, models = _paths(tmp_path)
        admin.create_server(
            "home", {"default_model": "old", "api_key": _KEY}, cfg, models
        )
        admin.create_server("work", {}, cfg, models)
        form = admin.get_servers(cfg)  # 폼을 연 시점
        # 그사이 방(agent-cli)이 대화형으로 고른 모델을 저장하고, 손으로 키를 하나 넣었다
        assert agent_cli_config.set_endpoint_value("home", "default_model", "picked")
        data = json.loads(cfg.read_text())
        data["endpoints"]["home"]["hand_key"] = 1
        data["top"] = True
        cfg.write_text(json.dumps(data))
        # 프런트는 바뀐 칸만 보낸다(base_url)
        admin.update_server("home", {"base_url": "http://new/v1"}, cfg)
        saved = json.loads(cfg.read_text())
        assert saved["endpoints"]["home"] == {
            "default_model": "picked",
            "api_key": _KEY,
            "hand_key": 1,
            "base_url": "http://new/v1",
        }
        assert saved["endpoints"]["work"] == {} and saved["top"] is True
        assert form["servers"][0]["default_model"] == "old"  # 낡은 폼은 덮지 않았다

    def test_masked_key_and_headers_are_kept(self, tmp_path):
        cfg, models = _paths(tmp_path)
        admin.create_server(
            "home",
            {"api_key": _KEY, "extra_headers": [{"name": "x-a", "value": _HDR}]},
            cfg,
            models,
        )
        admin.update_server(
            "home",
            {"api_key": "***", "extra_headers": [{"name": "x-a", "value": "***"}]},
            cfg,
        )
        saved = json.loads(cfg.read_text())["endpoints"]["home"]
        assert saved["api_key"] == _KEY
        assert saved["extra_headers"] == [
            {"name": "x-a", "value": _HDR, "enabled": True}
        ]

    def test_unknown_server_is_not_found(self, tmp_path):
        cfg, models = _paths(tmp_path)
        admin.create_server("home", {}, cfg, models)
        with pytest.raises(admin.AdminNotFound):
            admin.update_server("nope", {}, cfg)

    def test_frontend_sends_only_changed_fields(self):
        from pathlib import Path

        js = (Path(admin.__file__).parent / "static" / "admin.js").read_text()
        assert 'api("PUT", srvUrl(current), changedFields())' in js
        assert "loadedFields = allFields()" in js


class TestDefaultAndDelete:
    def test_make_default(self, tmp_path):
        cfg, models = _paths(tmp_path)
        admin.create_server("home", {}, cfg, models)
        admin.create_server("work", {}, cfg, models)
        admin.set_default_server("work", cfg)
        assert json.loads(cfg.read_text())["endpoint"] == "work"
        with pytest.raises(admin.AdminNotFound):
            admin.set_default_server("nope", cfg)

    def test_default_cannot_be_deleted(self, tmp_path):
        cfg, models = _paths(tmp_path)
        admin.create_server("home", {}, cfg, models)
        with pytest.raises(admin.AdminError, match="default server cannot be deleted"):
            admin.delete_server("home", cfg, models)
        assert "home" in json.loads(cfg.read_text())["endpoints"]

    def test_rooms_need_confirmation_and_models_go_too(self, tmp_path):
        cfg, models = _paths(tmp_path)
        admin.create_server("home", {}, cfg, models)
        admin.create_server("work", {}, cfg, models)
        for name in ("home", "work"):
            admin.save_model_entry(name, "m", {"dialect": "json_fc"}, models, cfg)
        with pytest.raises(admin.AdminConflict) as ei:
            admin.delete_server("work", cfg, models, rooms=3)
        assert ei.value.detail == {
            "reason": "in_use",
            "rooms": 3,
            "error": "3 room(s) use server 'work'",
        }
        admin.delete_server("work", cfg, models, rooms=3, confirm=True)
        assert list(json.loads(cfg.read_text())["endpoints"]) == ["home"]
        assert list(json.loads(models.read_text())["endpoints"]) == ["home"]


class TestServerModels:
    def test_models_are_per_server(self, tmp_path, monkeypatch):
        cfg, models = _paths(tmp_path)
        admin.create_server("home", {"base_url": "http://h/v1"}, cfg, models)
        admin.create_server("work", {"base_url": "http://w/v1"}, cfg, models)
        admin.save_model_entry("home", "a", {"dialect": "json_fc"}, models, cfg)
        admin.save_model_entry("work", "a", {"dialect": "xml_fc"}, models, cfg)
        seen = []

        def fake_get(url, headers=None, timeout=None):
            seen.append(url)
            return httpx.Response(
                200,
                json={"data": [{"id": "a"}, {"id": "b"}]},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(admin.httpx, "get", fake_get)
        view = admin.list_models_with_status("work", models, cfg)
        assert [(r["id"], r["dialect"], r["status"]) for r in view["models"]] == [
            ("a", "xml_fc", "served")
        ]
        assert view["new"] == ["b"] and seen == ["http://w/v1/models"]
        assert admin.delete_model_entry("work", "a", models) is True
        assert models_registry.model_binding(models, "home", "a") == "json_fc"

    def test_saving_a_model_for_a_missing_server_is_not_found(self, tmp_path):
        cfg, models = _paths(tmp_path)
        admin.create_server("home", {}, cfg, models)
        with pytest.raises(admin.AdminNotFound):
            admin.save_model_entry("nope", "a", {"dialect": "json_fc"}, models, cfg)

    def test_remote_probe_warning_is_per_server(self, tmp_path, monkeypatch, capsys):
        cfg, models = _paths(tmp_path)
        admin.create_server(
            "home", {"base_url": "http://127.0.0.1:9/v1", "api_key": _KEY}, cfg, models
        )
        admin.create_server(
            "work",
            {"base_url": "https://gw.example.com/v1", "api_key": _KEY},
            cfg,
            models,
        )
        _no_probe(monkeypatch)
        admin.list_served_models("home", cfg)
        assert capsys.readouterr().err == ""
        admin.list_served_models("work", cfg)
        err = capsys.readouterr().err
        assert "remote host" in err and "server 'work'" in err and _KEY not in err


# ── HTTP: admin·새 글·방 ───────────────────────────────────────────


def _blob(*responses) -> str:
    return "\n".join(json.dumps(r.json()) for r in responses)


class TestApi:
    def test_secrets_never_reach_the_browser(self, tmp_path, monkeypatch):
        _no_probe(monkeypatch, ["m"])
        _cfg, _store, _orch, c = _client(tmp_path)
        created = c.post(
            "/api/admin/endpoints",
            json={
                "name": "work",
                "base_url": f"https://gw.example.com/v1?token={_SECRET_QUERY}",
                "api_key": _KEY,
                "extra_headers": [{"name": "x-a", "value": _HDR}],
            },
        )
        _model(c, "work", "m")
        c.post("/api/posts", json={"topic": "t", "knobs": {"model": "m"}})
        updated = c.put("/api/admin/endpoints/work", json={"default_model": "m"})
        blob = _blob(
            created,
            updated,
            c.get("/api/admin/endpoints"),
            c.get("/api/admin/endpoints/work/models"),
            c.get("/api/endpoints"),
            c.get("/api/models?endpoint=work"),
            c.get("/api/posts"),
        )
        assert _KEY not in blob and _HDR not in blob
        # 비관리 목록은 호스트만 — 쿼리의 토큰도 없다(admin 만 base_url 전체를 보인다)
        assert _SECRET_QUERY not in _blob(c.get("/api/endpoints"), c.get("/api/posts"))
        assert c.get("/api/endpoints").json()["servers"][0]["host"] == "gw.example.com"

    def test_new_post_records_the_chosen_or_default_server(self, tmp_path):
        _cfg, store, _orch, c = _client(tmp_path)
        _server(c, "home")
        _server(c, "work")
        _model(c, "home", "m")
        _model(c, "work", "w")
        a = c.post("/api/posts", json={"topic": "a", "knobs": {"model": "m"}}).json()
        b = c.post(
            "/api/posts",
            json={"topic": "b", "endpoint": "work", "knobs": {"model": "w"}},
        ).json()
        assert store.get(a["post_id"]).endpoint == "home"
        assert store.get(b["post_id"]).endpoint == "work"
        assert {p["topic"]: p["endpoint"] for p in c.get("/api/posts").json()} == {
            "a": "home",
            "b": "work",
        }
        # 기본을 바꿔도 이미 만든 방은 그대로
        c.post("/api/admin/endpoints/work/default")
        assert store.get(a["post_id"]).endpoint == "home"

    def test_model_must_be_on_the_chosen_server(self, tmp_path):
        _cfg, store, _orch, c = _client(tmp_path)
        _server(c, "home")
        _server(c, "work")
        _model(c, "home", "m")
        r = c.post(
            "/api/posts",
            json={"topic": "t", "endpoint": "work", "knobs": {"model": "m"}},
        )
        assert r.status_code == 400
        assert r.json()["detail"] == {
            "reason": "dialect_unbound",
            "model": "m",
            "endpoint": "work",
        }
        assert store.list_posts() == []

    def test_unknown_or_no_server_is_400(self, tmp_path):
        _cfg, store, _orch, c = _client(tmp_path)
        r = c.post("/api/posts", json={"topic": "t", "knobs": {"model": "m"}})
        assert r.status_code == 400 and r.json()["detail"] == {"reason": "no_server"}
        _server(c, "home")
        r = c.post(
            "/api/posts",
            json={"topic": "t", "endpoint": "gone", "knobs": {"model": "m"}},
        )
        assert r.json()["detail"] == {"reason": "endpoint_missing", "endpoint": "gone"}
        assert store.list_posts() == []

    def test_deleted_server_blocks_open_with_409(self, tmp_path):
        _cfg, _store, orch, c = _client(tmp_path)
        _server(c, "home")
        _server(c, "work")
        _model(c, "work", "w")
        pid = c.post(
            "/api/posts",
            json={"topic": "t", "endpoint": "work", "knobs": {"model": "w"}},
        ).json()["post_id"]
        r = c.delete("/api/admin/endpoints/work")
        assert r.status_code == 409 and r.json()["detail"]["rooms"] == 1
        assert c.delete("/api/admin/endpoints/work?confirm=true").status_code == 200
        r = c.post(f"/api/posts/{pid}/open")
        assert r.status_code == 409
        assert r.json()["detail"] == {"reason": "endpoint_missing", "endpoint": "work"}
        assert orch.opened == []
        row = c.get("/api/posts").json()[0]
        assert row["model_bound"] is False
        assert row["problem"]["reason"] == "endpoint_missing"

    def test_default_server_delete_is_400(self, tmp_path):
        _cfg, _store, _orch, c = _client(tmp_path)
        _server(c, "home")
        r = c.delete("/api/admin/endpoints/home?confirm=true")
        assert r.status_code == 400 and "default" in r.json()["detail"]

    def test_admin_view_counts_rooms_and_models(self, tmp_path):
        _cfg, _store, _orch, c = _client(tmp_path)
        _server(c, "home")
        _server(c, "work")
        _model(c, "work", "w")
        _model(c, "work", "x")
        c.post(
            "/api/posts",
            json={"topic": "t", "endpoint": "work", "knobs": {"model": "w"}},
        )
        view = c.get("/api/admin/endpoints").json()
        rows = {
            s["name"]: (s["models"], s["rooms"], s["default"]) for s in view["servers"]
        }
        assert rows == {"home": (0, 0, True), "work": (2, 1, False)}

    def test_models_api_is_per_server(self, tmp_path):
        _cfg, _store, _orch, c = _client(tmp_path)
        _server(c, "home")
        _server(c, "work")
        _model(c, "home", "m")
        _model(c, "work", "w", "xml_fc")
        assert [m["id"] for m in c.get("/api/models").json()] == ["m"]  # 기본 서버
        assert [m["id"] for m in c.get("/api/models?endpoint=work").json()] == ["w"]

    def test_cli_gate(self, tmp_path, monkeypatch):
        _cfg, _store, _orch, c = _client(tmp_path)
        monkeypatch.setattr(instances, "cli_version", lambda _bin: "10.143.0")
        gate = c.get("/api/admin/endpoints").json()["cli"]
        assert gate == {
            "version": "10.143.0",
            "supports_endpoints": False,
            "required": "10.144.0",
        }
        monkeypatch.setattr(instances, "cli_version", lambda _bin: "10.144.0")
        assert c.get("/api/admin/endpoints").json()["cli"]["supports_endpoints"] is True


class TestRoomServer:
    def _cfg(self, tmp_path):
        return Config(data_dir=tmp_path / "d", workspaces_root=tmp_path / "w")

    @pytest.mark.parametrize(
        ("version", "endpoint", "passed"),
        [
            ("10.144.0", "work", True),
            ("10.143.9", "work", False),  # 모르는 옵션은 부트를 죽인다
            ("10.144.0", "", False),  # 기록 없는 옛 글
            (None, "work", False),
        ],
    )
    def test_spawn_passes_the_recorded_server(
        self, tmp_path, monkeypatch, version, endpoint, passed
    ):
        monkeypatch.setattr(instances, "cli_version", lambda _bin: version)
        post = Post(post_id="p1", topic="t", knobs={"model": "m"}, endpoint=endpoint)
        cmd = instances.build_spawn_cmd(self._cfg(tmp_path), post, port=1, token="x")
        assert (
            (
                ["--endpoint", "work"]
                == cmd[cmd.index("--endpoint") : cmd.index("--endpoint") + 2]
            )
            if passed
            else ("--endpoint" not in cmd)
        )

    def test_store_round_trip_and_old_db(self, tmp_path):
        import sqlite3

        db = tmp_path / "board.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE posts (post_id TEXT PRIMARY KEY, topic TEXT NOT NULL, "
            "session_id TEXT UNIQUE, knobs TEXT, force_active INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL, last_opened_at TEXT);"
            "INSERT INTO posts (post_id, topic, created_at) VALUES ('old', 'o', 'x');"
        )
        conn.commit()
        conn.close()
        store = Store(db)
        assert store.get("old").endpoint == ""  # 옛 행 = 기록 없음
        p = store.create_post(topic="n", endpoint="work")
        assert store.get(p.post_id).endpoint == "work"
        store.set_endpoint("old", "home")
        assert store.get("old").endpoint == "home"

    def test_mirror_learns_the_server_of_an_old_room_only(self, tmp_path, monkeypatch):
        from agent_board import sessions

        _cfg, store, _orch, c = _client(tmp_path)
        _server(c, "home")
        _server(c, "work")
        old = store.create_post(topic="old", knobs={"model": "m"})
        new = store.create_post(topic="new", knobs={"model": "m"}, endpoint="home")
        monkeypatch.setattr(
            sessions,
            "live_state",
            lambda ws, sid: {
                "status": "running",
                "awaiting_input": False,
                "viewers": 0,
                "endpoint": "work",
            },
        )
        c.get("/api/posts")
        assert store.get(old.post_id).endpoint == "work"  # 기록 없던 글은 배운다
        assert store.get(new.post_id).endpoint == "home"  # 기록된 글은 그대로

    def test_live_state_reads_the_endpoint_from_web_json(self, tmp_path, monkeypatch):
        """agent-cli 가 쓰는 web.json 의 키를 보드가 그대로 읽는다 (계약)."""
        from agent_cli.instance_file import write_instance_file

        from agent_board import sessions

        sdir = tmp_path / ".agent-cli" / "sessions" / "s1"
        write_instance_file(
            sdir, session_id="s1", host="127.0.0.1", port=1, token="t", endpoint="work"
        )
        monkeypatch.setattr(instances, "pid_alive", lambda pid: True)
        monkeypatch.setattr(
            instances, "read_status_json", lambda ws, sid: {"busy": False}
        )
        assert sessions.live_state(tmp_path, "s1")["endpoint"] == "work"

    def test_clone_carries_the_chosen_server(self, tmp_path):
        _cfg, store, _orch, c = _client(tmp_path)
        _server(c, "home")
        _server(c, "work")
        _model(c, "home", "m")
        _model(c, "work", "w")
        src = c.post(
            "/api/posts",
            json={"topic": "s", "endpoint": "work", "knobs": {"model": "w"}},
        ).json()
        dst = c.post(
            "/api/posts",
            json={
                "topic": "c",
                "endpoint": "work",
                "knobs": {"model": "w"},
                "clone_from": src["post_id"],
            },
        ).json()
        assert store.get(dst["post_id"]).endpoint == "work"


class TestWithoutAgentCli:
    """보드와 agent-cli 가 다른 venv 인 배포 — 새 글 폼의 표면은 agent-cli import 없이 돈다."""

    def test_endpoints_api_lists_servers_without_agent_cli(self, tmp_path, monkeypatch):
        cfg_json, _models = _paths(tmp_path)
        _write(
            cfg_json,
            {
                "endpoint": "home",
                "endpoints": {
                    "home": {
                        "base_url": f"https://u:p@gw.example.com:8443/v1?t={_SECRET_QUERY}",
                        "api_key": _KEY,
                        "default_model": "m",
                    }
                },
            },
        )
        monkeypatch.setitem(sys.modules, "agent_cli", None)
        monkeypatch.setitem(sys.modules, "agent_cli.config", None)
        _cfg, _store, _orch, c = _client(tmp_path)
        r = c.get("/api/endpoints")
        assert r.status_code == 200
        assert r.json() == {
            "default": "home",
            "servers": [
                {
                    "name": "home",
                    "host": "gw.example.com:8443",
                    "default": True,
                    "default_model": "m",
                }
            ],
        }
        assert _KEY not in r.text and _SECRET_QUERY not in r.text
        assert c.get("/api/models").status_code == 200

    def test_models_registry_does_not_import_agent_cli(self):
        import ast
        from pathlib import Path

        tree = ast.parse(Path(models_registry.__file__).read_text(encoding="utf-8"))
        imported = [
            n.module if isinstance(n, ast.ImportFrom) else a.name
            for n in ast.walk(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))
            for a in (n.names if isinstance(n, ast.Import) else [n])
        ]
        assert not [m for m in imported if m and m.startswith("agent_cli")]


# ── agent-cli 와의 계약 (교차 저장소) ─────────────────────────────────


class TestAgentCliContract:
    def test_board_written_files_are_what_agent_cli_reads(
        self, tmp_path, agent_cli_config
    ):
        cfg, models = _paths(tmp_path)
        admin.create_server(
            "home",
            {"base_url": "http://h/v1", "api_key": _KEY, "default_model": "m"},
            cfg,
            models,
        )
        admin.create_server(
            "work", {"provider": "anthropic", "base_url": "http://w/v1"}, cfg, models
        )
        admin.save_model_entry(
            "work", "w", {"dialect": "xml_fc", "context_window": 9}, models, cfg
        )
        cli = agent_cli_config
        assert cli.default_endpoint_name() == "home"
        assert sorted(cli.list_endpoints()) == ["home", "work"]
        assert cli.endpoint_config("home") == {
            "base_url": "http://h/v1",
            "api_key": _KEY,
            "default_model": "m",
        }
        cli.use_endpoint("work")
        assert cli.get_model_entry("w") == {"dialect": "xml_fc", "context_window": 9}

    def test_agent_cli_written_files_are_what_the_board_reads(
        self, tmp_path, agent_cli_config, monkeypatch
    ):
        cli = agent_cli_config
        cli.save_endpoint("lab", {"base_url": "http://l/v1", "api_key": _KEY})
        cli.set_default_endpoint("lab")
        cli.use_endpoint("lab")
        assert cli.save_model_entry(
            "auto", {"dialect": "json_fc", "_auto_detected": True}
        )
        cfg, models = _paths(tmp_path)
        names, default = models_registry.server_names(cfg)
        assert (names, default) == (["lab"], "lab")
        assert models_registry.model_binding(models, "lab", "auto") == "json_fc"
        view = admin.get_servers(cfg, models)
        assert view["servers"][0]["api_key"] == "***" and _KEY not in json.dumps(view)

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000/v1",
            "https://gw.example.com/v1?token=secret",
            "https://user:pw@gw.example.com:8443/x/y",
            "http://[::1]:9/v1",
            "http://[fe80::1]/v1",
            "https://gw.example.com",
            "http://h:99999/v1",  # 깨진 포트 — ValueError
            "http://h:abc/v1",
            "not a url",
            "",
        ],
    )
    def test_board_endpoint_host_matches_agent_cli(self, url, agent_cli_config):
        """보드의 stdlib 판(새 글 폼용)과 agent-cli 의 규칙이 같다."""
        assert models_registry.endpoint_host(url) == agent_cli_config.endpoint_host(url)

    def test_legacy_predicate_matches(self, agent_cli_config):
        """보드의 "옛 모양 = 서버 없음" 판정이 agent-cli 의 것과 같다."""
        samples = [
            {},
            {"provider": "openai"},
            {"models": {}},
            {"endpoints": {}},
            {"endpoint": "a", "endpoints": {"a": {}}},
        ]
        for data in samples:
            board_sees_servers = "endpoints" in data
            assert board_sees_servers == (
                not agent_cli_config.is_legacy_shape(data) and bool(data)
            )

    def test_the_flag_the_board_passes_exists(self):
        from agent_cli.main import app
        from typer.testing import CliRunner

        out = CliRunner().invoke(app, ["web", "--help"], env={"COLUMNS": "200"}).output
        # typer 는 GITHUB_ACTIONS 에서 도움말 색을 강제한다(env 로 끌 수 없다) — 글자 사이의
        # ANSI 를 벗기고 본다
        out = re.sub(r"\x1b\[[0-9;]*m", "", out)
        assert "--endpoint" in out
        assert instances.ENDPOINTS_CLI <= tuple(
            int(x) for x in __import__("agent_cli").__version__.split(".")
        )
