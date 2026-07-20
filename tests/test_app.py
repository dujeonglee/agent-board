"""Board HTTP API + UI wiring (DESIGN §3/§10).

create_app builds the real Store but accepts injected orchestrator/keepalive so
the create/list/delete/force-active endpoints are tested without spawning real
agent-cli instances.
"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from agent_board.app import acquire_singleton_lock, create_app, gateway_banner
from agent_board.config import Config
from agent_board.store import Store


class FakeOrch:
    def __init__(self, change_result=None):
        self.opened = []
        self.restarted = []
        self.model_changes = []
        self._change_result = change_result or {"ok": True, "changed": True}

    async def open(self, post_id):
        self.opened.append(post_id)
        return f"/s/{post_id}/"

    async def restart(self, post_id):
        self.restarted.append(post_id)
        return f"/s/{post_id}/?token=T"

    async def change_model(self, post_id, model_id):
        self.model_changes.append((post_id, model_id))
        return self._change_result


class FakeKeepalive:
    def __init__(self):
        self.enabled = []
        self.disabled = []

    async def enable(self, post_id):
        self.enabled.append(post_id)

    async def disable(self, post_id):
        self.disabled.append(post_id)


def _client(tmp_path, *, orch=None, keepalive=None):
    cfg = Config(data_dir=tmp_path / "data", workspaces_root=tmp_path / "ws")
    store = Store(cfg.db_path)
    app = create_app(
        cfg,
        store=store,
        orchestrator=orch or FakeOrch(),
        keepalive=keepalive or FakeKeepalive(),
    )
    return cfg, store, TestClient(app)


class TestPostsApi:
    def test_index_served(self, tmp_path):
        _, _, c = _client(tmp_path)
        r = c.get("/")
        assert r.status_code == 200 and "agent-board" in r.text.lower()

    def test_list_empty(self, tmp_path):
        _, _, c = _client(tmp_path)
        assert c.get("/api/posts").json() == []

    def test_create_makes_workspace_and_lists(self, tmp_path):
        cfg, _, c = _client(tmp_path)
        r = c.post("/api/posts", json={"topic": "DOOM 만들기"})
        assert r.status_code == 200
        pid = r.json()["post_id"]
        assert cfg.workspace_for(pid).is_dir()  # workspace created
        posts = c.get("/api/posts").json()
        assert len(posts) == 1
        assert posts[0]["topic"] == "DOOM 만들기"
        assert posts[0]["status"] == "idle"  # never opened
        assert posts[0]["last_query"] is None
        assert posts[0]["created_at"]  # creation date present
        assert posts[0]["last_query_at"] is None  # no query yet
        assert posts[0]["awaiting_input"] is False  # not waiting on input

    def test_create_ignores_directive_and_writes_no_file(self, tmp_path):
        # The board no longer writes DIRECTIVE.md — a stray ``directive`` key is
        # ignored (extra field) and nothing is written to the workspace.
        cfg, _, c = _client(tmp_path)
        r = c.post("/api/posts", json={"topic": "t", "directive": "항상 한국어로"})
        assert r.status_code == 200
        pid = r.json()["post_id"]
        assert not (cfg.workspace_for(pid) / ".agent-cli" / "DIRECTIVE.md").exists()

    def test_create_requires_topic(self, tmp_path):
        _, _, c = _client(tmp_path)
        assert c.post("/api/posts", json={}).status_code == 422

    def test_delete_removes_post_and_workspace(self, tmp_path):
        cfg, _, c = _client(tmp_path)
        pid = c.post("/api/posts", json={"topic": "t"}).json()["post_id"]
        ws = cfg.workspace_for(pid)
        assert ws.is_dir()
        assert c.delete(f"/api/posts/{pid}").status_code == 200
        assert not ws.exists()  # workspace dir removed (board-owned)
        assert c.get("/api/posts").json() == []

    def test_delete_missing_404(self, tmp_path):
        _, _, c = _client(tmp_path)
        assert c.delete("/api/posts/nope").status_code == 404

    def test_delete_deregisters_route(self, tmp_path):
        # else a deleted post leaves a dangling /s/<id> gateway route (found in
        # real-Caddy e2e: remove_route was never wired into delete).
        class SpyRouter:
            def __init__(self):
                self.removed = []

            def mount(self, app):
                pass

            def ensure_route(self, post_id, port):
                pass

            def remove_route(self, post_id):
                self.removed.append(post_id)

        cfg = Config(data_dir=tmp_path / "d", workspaces_root=tmp_path / "w")
        store = Store(cfg.db_path)
        spy = SpyRouter()
        app = create_app(
            cfg,
            store=store,
            router=spy,
            orchestrator=FakeOrch(),
            keepalive=FakeKeepalive(),
        )
        client = TestClient(app)
        pid = client.post("/api/posts", json={"topic": "t"}).json()["post_id"]
        client.delete(f"/api/posts/{pid}")
        assert spy.removed == [pid]

    def test_open_calls_orchestrator(self, tmp_path):
        orch = FakeOrch()
        cfg, _, c = _client(tmp_path, orch=orch)
        pid = c.post("/api/posts", json={"topic": "t"}).json()["post_id"]
        r = c.post(f"/api/posts/{pid}/open")
        assert r.status_code == 200
        assert r.json()["url"] == f"/s/{pid}/"
        assert orch.opened == [pid]

    def test_open_missing_404(self, tmp_path):
        _, _, c = _client(tmp_path)
        assert c.post("/api/posts/nope/open").status_code == 404

    def test_restart_calls_orchestrator(self, tmp_path):
        orch = FakeOrch()
        cfg, _, c = _client(tmp_path, orch=orch)
        pid = c.post("/api/posts", json={"topic": "t"}).json()["post_id"]
        r = c.post(f"/api/posts/{pid}/restart")
        assert r.status_code == 200
        assert r.json()["url"] == f"/s/{pid}/?token=T"
        assert orch.restarted == [pid]

    def test_restart_missing_404(self, tmp_path):
        _, _, c = _client(tmp_path)
        assert c.post("/api/posts/nope/restart").status_code == 404

    def test_force_active_on_off(self, tmp_path):
        ka = FakeKeepalive()
        cfg, store, c = _client(tmp_path, keepalive=ka)
        pid = c.post("/api/posts", json={"topic": "t"}).json()["post_id"]

        c.post(f"/api/posts/{pid}/force_active", json={"enabled": True})
        assert store.get(pid).force_active is True
        assert ka.enabled == [pid]

        c.post(f"/api/posts/{pid}/force_active", json={"enabled": False})
        assert store.get(pid).force_active is False
        assert ka.disabled == [pid]


class TestGatewaySelection:
    def _proxy_paths(self, tmp_path, gateway):
        cfg = Config(
            data_dir=tmp_path / "d", workspaces_root=tmp_path / "w", gateway=gateway
        )
        store = Store(cfg.db_path)
        app = create_app(
            cfg, store=store, orchestrator=FakeOrch(), keepalive=FakeKeepalive()
        )
        # the /s/<id> reverse-proxy catch-all (NOT the /api/posts/{post_id} API)
        return [
            p for r in app.routes if (p := getattr(r, "path", "")).startswith("/s/")
        ]

    def test_caddy_gateway_mounts_revive_fallback(self, tmp_path):
        # Caddy proxies live /s/<id> itself, but the board mounts a GET/HEAD
        # revive handler for the fall-through (route dropped on death → reopen).
        # Behaviour (302/503) is covered in test_caddy_router.TestCaddyRevive.
        assert self._proxy_paths(tmp_path, "caddy")  # non-empty (revive routes)

    def test_board_proxy_gateway_mounts_catchall(self, tmp_path):
        assert self._proxy_paths(tmp_path, "board-proxy")  # non-empty


class TestModelSelection:
    def test_api_models_lists_registry(self, tmp_path):
        import json

        reg = tmp_path / "models.json"
        reg.write_text(json.dumps({"models": {"Qwen-X": {"provider": "omlx"}}}))
        cfg = Config(
            data_dir=tmp_path / "d", workspaces_root=tmp_path / "w", models_json=reg
        )
        store = Store(cfg.db_path)
        c = TestClient(
            create_app(
                cfg, store=store, orchestrator=FakeOrch(), keepalive=FakeKeepalive()
            )
        )
        out = c.get("/api/models").json()
        assert out == [{"id": "Qwen-X", "provider": "omlx", "context_window": None}]

    def test_create_with_model_id_persists(self, tmp_path):
        cfg, store, c = _client(tmp_path)
        pid = c.post("/api/posts", json={"topic": "t", "model_id": "Qwen-X"}).json()[
            "post_id"
        ]
        assert store.get(pid).model_id == "Qwen-X"
        assert c.get("/api/posts").json()[0]["model_id"] == "Qwen-X"

    def test_create_without_model_id_is_none(self, tmp_path):
        _, store, c = _client(tmp_path)
        pid = c.post("/api/posts", json={"topic": "t"}).json()["post_id"]
        assert store.get(pid).model_id is None


class TestUiWired:
    def test_static_and_js_wired(self, tmp_path):
        _, _, c = _client(tmp_path)
        html = c.get("/").text
        js = c.get("/static/app.js").text
        # post list container + new-post form + the actions
        assert 'id="posts"' in html
        assert 'id="new-topic"' in html
        assert "api/posts" in js  # list/create
        assert "/open" in js and "force_active" in js
        assert "confirm(" in js  # delete is destructive
        # 3-state status indicator (working/running/idle)
        assert "working" in js and "응답 중" in js
        css = c.get("/static/style.css").text
        assert "dot.busy" in css
        assert "@media" in css  # responsive (mobile) layout
        assert 'name="viewport"' in html
        # per-post model selection
        assert 'id="new-model"' in html
        assert "/api/models" in js and "model_id" in js
        # created / last-query dates on the card
        assert "post-meta" in js and "created_at" in js and "last_query_at" in js
        # "needs your answer" indicator for pending ask/confirm
        assert "awaiting_input" in js and "응답 필요" in js
        # open in new tab by default + "현재 페이지에서 열기" toggle
        assert 'id="same-tab"' in html and "현재 페이지에서 열기" in html
        assert "window.open" in js
        assert "라이브러리 만들기" in html  # topic placeholder
        assert "글 하나 = agent-cli 세션" not in html  # hint removed


class TestChangeModelApi:
    def test_change_model_ok(self, tmp_path):
        orch = FakeOrch()
        _, _, c = _client(tmp_path, orch=orch)
        pid = c.post("/api/posts", json={"topic": "t"}).json()["post_id"]
        r = c.post(f"/api/posts/{pid}/model", json={"model_id": "Qwen3.6"})
        assert r.status_code == 200
        assert orch.model_changes == [(pid, "Qwen3.6")]

    def test_change_model_blocked_is_409(self, tmp_path):
        # gate refused (busy / someone watching) → 409 with the reason
        orch = FakeOrch(change_result={"ok": False, "reason": "busy"})
        _, _, c = _client(tmp_path, orch=orch)
        pid = c.post("/api/posts", json={"topic": "t"}).json()["post_id"]
        r = c.post(f"/api/posts/{pid}/model", json={"model_id": "x"})
        assert r.status_code == 409 and r.json()["detail"] == "busy"

    def test_change_model_unknown_post_404(self, tmp_path):
        _, _, c = _client(tmp_path)
        assert (
            c.post("/api/posts/nope/model", json={"model_id": "x"}).status_code == 404
        )

    def test_post_view_exposes_viewers_and_changeable(self, tmp_path):
        _, _, c = _client(tmp_path)
        pid = c.post("/api/posts", json={"topic": "t"}).json()["post_id"]
        p = c.get("/api/posts").json()[0]
        assert p["post_id"] == pid
        assert p["viewers"] == 0
        # never opened → idle (down) → model is changeable
        assert p["model_changeable"] is True


class TestEventsStream:
    def test_events_route_registered_as_stream(self, tmp_path):
        # The SSE stream is infinite, so we don't consume it via TestClient
        # (that blocks) — the push/scan behaviour is covered in test_live_events.
        # Here we only assert the route exists and is wired to the app.
        from agent_board.app import create_app

        cfg = Config(data_dir=tmp_path / "data", workspaces_root=tmp_path / "ws")
        app = create_app(
            cfg,
            store=Store(cfg.db_path),
            orchestrator=FakeOrch(),
            keepalive=FakeKeepalive(),
        )
        paths = {r.path for r in app.routes}
        assert "/api/events" in paths

    def test_html_wires_sse_not_polling(self, tmp_path):
        _, _, c = _client(tmp_path)
        js = c.get("/static/app.js").text
        assert 'new EventSource("/api/events")' in js
        assert "setInterval(load" not in js  # the 5s poll is gone

    def test_lifespan_closes_router(self, tmp_path):
        # board shutdown must release the router's httpx client (aclose) — else
        # a persistent client leaks on every board restart.
        closed = []

        class SpyRouter:
            def mount(self, app):
                pass

            def ensure_route(self, post_id, port):
                pass

            def remove_route(self, post_id):
                pass

            async def aclose(self):
                closed.append(True)

        cfg = Config(data_dir=tmp_path / "d", workspaces_root=tmp_path / "w")
        app = create_app(
            cfg,
            store=Store(cfg.db_path),
            router=SpyRouter(),
            orchestrator=FakeOrch(),
            keepalive=FakeKeepalive(),
        )
        with TestClient(app):  # enter + exit lifespan (startup/shutdown)
            pass
        assert closed == [True]


class TestGatewayBanner:
    def test_board_proxy_default(self, tmp_path):
        cfg = Config(data_dir=tmp_path / "d", workspaces_root=tmp_path / "w")
        assert "board-proxy" in gateway_banner(cfg)
        assert "caddy" not in gateway_banner(cfg).lower()

    def test_caddy_shows_admin(self, tmp_path):
        cfg = Config(
            data_dir=tmp_path / "d",
            workspaces_root=tmp_path / "w",
            gateway="caddy",
            caddy_admin="http://127.0.0.1:2019",
        )
        b = gateway_banner(cfg)
        assert b.startswith("caddy") and "127.0.0.1:2019" in b


class TestClonePost:
    """대화방 clone (v1.20.0) — 트리 엔드포인트 + POST /api/posts 확장."""

    def _seed_source(self, cfg, store, *, with_session=True):
        import json as _json

        post = store.create_post(topic="source")
        ws = cfg.workspace_for(post.post_id)
        ws.mkdir(parents=True)
        (ws / "main.py").write_text("print('hi')")
        if with_session:
            sdir = ws / ".agent-cli" / "sessions" / "1111111111"
            sdir.mkdir(parents=True)
            (sdir / "session.jsonl").write_text(
                _json.dumps(
                    {
                        "_meta": {
                            "session_id": "1111111111",
                            "workspace": str(ws),
                            "updated_at": "x",
                            "response_format": "json_fc",
                        }
                    }
                )
                + "\n"
            )
            (sdir / "history.jsonl").write_text('{"role":"user"}\n')
            (sdir / "web.json").write_text('{"pid":999}')
        return post

    def test_tree_lists_source_workspace(self, tmp_path):
        cfg, store, c = _client(tmp_path)
        src = self._seed_source(cfg, store)
        tree = c.get(f"/api/posts/{src.post_id}/tree").json()
        names = [e["name"] for e in tree]
        assert "main.py" in names and ".agent-cli" in names
        # 하위 레벨
        sub = c.get(
            f"/api/posts/{src.post_id}/tree", params={"path": ".agent-cli/sessions"}
        ).json()
        assert [e["name"] for e in sub] == ["1111111111"]

    def test_tree_unknown_post_404(self, tmp_path):
        _, _, c = _client(tmp_path)
        assert c.get("/api/posts/nope/tree").status_code == 404

    def test_clone_files_only_fresh_session(self, tmp_path):
        cfg, store, c = _client(tmp_path)
        src = self._seed_source(cfg, store)
        r = c.post(
            "/api/posts",
            json={
                "topic": "cloned",
                "clone_from": src.post_id,
                "clone_paths": ["main.py"],
            },
        )
        assert r.status_code == 200
        new_id = r.json()["post_id"]
        new_ws = cfg.workspace_for(new_id)
        assert (new_ws / "main.py").read_text() == "print('hi')"
        # 세션 안 가져옴 → session_id 없음(fresh)
        assert store.get(new_id).session_id is None

    def test_clone_with_session_resumes(self, tmp_path, monkeypatch):
        import json as _json

        from agent_board import app as app_mod

        cfg, store, c = _client(tmp_path)
        src = self._seed_source(cfg, store)
        monkeypatch.setattr(app_mod, "_new_session_id", lambda: "2222222222")
        r = c.post(
            "/api/posts",
            json={
                "topic": "cloned",
                "clone_from": src.post_id,
                "clone_paths": [".agent-cli"],
            },
        )
        assert r.status_code == 200
        new_id = r.json()["post_id"]
        # 세션 remap → set_session_id 로 --resume 배선
        assert store.get(new_id).session_id == "2222222222"
        newdir = cfg.workspace_for(new_id) / ".agent-cli" / "sessions" / "2222222222"
        assert newdir.is_dir()
        assert not (newdir / "web.json").exists()  # 사이드카 제외
        header = _json.loads((newdir / "session.jsonl").read_text().splitlines()[0])
        assert header["_meta"]["session_id"] == "2222222222"
        assert header["_meta"]["workspace"] == str(cfg.workspace_for(new_id).resolve())

    def test_clone_session_id_conflict_downgrades_to_fresh(self, tmp_path, monkeypatch):
        """set_session_id 가 (sid 충돌 등) 실패하면 조용히 fresh 로 강등 —
        파일은 복사되되 --resume 안 됨(200 유지, session_id None). v1.21.1
        감사: 이 강등 경로가 무테스트였음."""
        cfg, store, c = _client(tmp_path)
        src = self._seed_source(cfg, store)
        # store.set_session_id 를 실패시켜 강등 경로를 강제
        monkeypatch.setattr(
            store,
            "set_session_id",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sid clash")),
        )
        r = c.post(
            "/api/posts",
            json={
                "topic": "강등본",
                "clone_from": src.post_id,
                "clone_paths": [".agent-cli"],
            },
        )
        assert r.status_code == 200  # 크래시 아님
        new_id = r.json()["post_id"]
        assert store.get(new_id).session_id is None  # fresh 강등
        # 파일(.agent-cli)은 복사됨
        assert (cfg.workspace_for(new_id) / ".agent-cli").is_dir()

    def test_clone_paths_without_source_400(self, tmp_path):
        _, _, c = _client(tmp_path)
        r = c.post("/api/posts", json={"topic": "x", "clone_paths": ["a"]})
        assert r.status_code == 400

    def test_clone_unknown_source_404(self, tmp_path):
        _, _, c = _client(tmp_path)
        r = c.post(
            "/api/posts",
            json={"topic": "x", "clone_from": "nope", "clone_paths": ["a"]},
        )
        assert r.status_code == 404

    def test_clone_traversal_rejected_and_rolled_back(self, tmp_path):
        cfg, store, c = _client(tmp_path)
        src = self._seed_source(cfg, store)
        (tmp_path / "secret").write_text("s")
        before = len(store.list_posts())
        r = c.post(
            "/api/posts",
            json={
                "topic": "x",
                "clone_from": src.post_id,
                "clone_paths": ["../../secret"],
            },
        )
        assert r.status_code == 400
        # 방 롤백 (orphan 없음)
        assert len(store.list_posts()) == before

    def test_plain_create_still_works(self, tmp_path):
        _, store, c = _client(tmp_path)
        r = c.post("/api/posts", json={"topic": "plain"})
        assert r.status_code == 200 and store.get(r.json()["post_id"]) is not None

    def test_frontend_clone_modal_wired(self, tmp_path):
        """v1.21.0: 각 글 카드 복제 버튼 → 모달. 팝업 한 창에 전부."""
        _, _, c = _client(tmp_path)
        html = c.get("/").text
        js = c.get("/static/app.js").text
        css = c.get("/static/style.css").text
        assert 'id="clone-dlg"' in html and 'id="clone-tree"' in html
        assert 'id="clone-topic"' in html and 'id="clone-model"' in html
        assert "openCloneDialog" in js and "showModal" in js
        assert "clone_from" in js and "clone_paths" in js
        assert "cloneSelection" in js  # 조상-dedupe 선택 수집
        assert "closeCloneDialog" in js  # 취소/닫기 = 중단
        assert "구버전" in js  # 404=구버전 서버 안내
        assert "dialog#clone-dlg" in css and "::backdrop" in css
        # 카드에 복제 버튼 배선
        assert '"clone btn-ghost"' in js and ".clone" in js

    def test_admin_no_removed_structured_strict_fields(self, tmp_path):
        """v1.14.0 에서 제거된 structured/strict capability 필드가 admin
        에 재유입되지 않게 (v6/v7 dead 필드). v1.21.1 감사 #5."""
        _, _, c = _client(tmp_path)
        html = c.get("/static/admin.html").text
        js = c.get("/static/admin.js").text
        assert "ef-structured" not in js and "ef-strict" not in js
        assert "supports_structured_output" not in js
        assert "structured" not in html  # <th>structured</th> 컬럼도 제거됨


class TestAgentsInPostView:
    """v1.17.0: 상주 에이전트 요약(status.json `agents`, agent-cli
    ≥7.10.0)을 행에 노출 — 🤖 W/N 칩 + main 유휴·에이전트 작업 중이면
    "에이전트 작업 중" 상태(동일 원형 dot, 색만 구분)."""

    def test_post_view_passes_agents_through(self, tmp_path, monkeypatch):
        from agent_board import sessions

        cfg, store, c = _client(tmp_path)
        store.create_post(topic="t")
        monkeypatch.setattr(
            sessions,
            "live_state",
            lambda ws, sid: {
                "status": "running",
                "awaiting_input": False,
                "viewers": 0,
                "agents": {
                    "alive": 2,
                    "working": 1,
                    "list": [
                        {
                            "key": "agt-1",
                            "profile": "coder",
                            "name": "ui",
                            "state": "working",
                        },
                        {
                            "key": "agt-2",
                            "profile": "reviewer",
                            "name": "",
                            "state": "idle",
                        },
                    ],
                },
            },
        )
        posts = c.get("/api/posts").json()
        assert posts[0]["agents"]["alive"] == 2
        assert posts[0]["agents"]["working"] == 1

    def test_post_view_agents_absent_when_not_provided(self, tmp_path, monkeypatch):
        from agent_board import sessions

        cfg, store, c = _client(tmp_path)
        store.create_post(topic="t")
        monkeypatch.setattr(
            sessions,
            "live_state",
            lambda ws, sid: {"status": "idle", "awaiting_input": False, "viewers": 0},
        )
        posts = c.get("/api/posts").json()
        assert posts[0].get("agents") is None

    def test_frontend_agents_chip_wired(self, tmp_path):
        _, _, c = _client(tmp_path)
        js = c.get("/static/app.js").text
        css = c.get("/static/style.css").text
        assert "agents-chip" in js
        assert "agents-busy" in js  # main 유휴 + 에이전트 작업 중 상태
        assert "에이전트 작업" in js
        assert ".dot.agents-busy" in css
        assert "--agent-work" in css


class TestDeathEdgeRouteWiring:
    """★배선 합동 검증 (v1.18.1 감사): app 이 LiveEvents 에 on_death=
    router.remove_route 를 실제로 넘기는지 — 양쪽 반쪽 유닛(TestOnDeath
    는 mock 콜백, delete 테스트는 별개 경로)만으로는 이 한 줄 배선이
    빠져도 전부 green 이라 caddy 502-고착이 재발할 수 있다."""

    def test_death_edge_calls_router_remove_route(self, tmp_path, monkeypatch):
        import json as _json

        from agent_board import instances
        from agent_board.router import Router

        class SpyRouter(Router):
            def __init__(self):
                self.removed = []

            def ensure_route(self, post_id, port):
                pass

            def remove_route(self, post_id):
                self.removed.append(post_id)

            def set_reopen(self, reopen):
                pass

            def mount(self, app):
                pass

            async def aclose(self):
                pass

        cfg = Config(data_dir=tmp_path / "data", workspaces_root=tmp_path / "ws")
        store = Store(cfg.db_path)
        spy = SpyRouter()
        app = create_app(
            cfg,
            store=store,
            router=spy,
            orchestrator=FakeOrch(),
            keepalive=FakeKeepalive(),
        )
        live = app.state.live_events

        post = store.create_post(topic="t")
        store.set_session_id(post.post_id, "S1")
        sdir = cfg.workspace_for(post.post_id) / ".agent-cli" / "sessions" / "S1"
        sdir.mkdir(parents=True)
        (sdir / "web.json").write_text(
            _json.dumps({"session_id": "S1", "pid": 4242, "port": 9000})
        )

        alive = {"v": True}
        monkeypatch.setattr(instances, "pid_alive", lambda pid: alive["v"])
        live._prime()
        alive["v"] = False  # 인스턴스 사망
        live._scan()
        assert spy.removed == [post.post_id]


class TestTabGuard:
    """브라우저-로컬 탭 가드 (v1.14.0) — board-proxy 게이트웨이에서 방/대시
    보드 탭들이 origin 당 6연결(HTTP/1.1) 풀을 소진해 승인 클릭까지
    stall 하던 실사고(agent-cli v7.2.0 참조)의 예방. 프런트가 열기 전에
    BroadcastChannel 로 보유 탭 수를 세고, caddy(h2) 모드에선 자동 해제."""

    def test_gateway_endpoint_reports_mode(self, tmp_path):
        _, _, c = _client(tmp_path)
        r = c.get("/api/gateway")
        assert r.status_code == 200
        assert r.json() == {"gateway": "board-proxy"}

    def test_gateway_endpoint_reports_caddy(self, tmp_path):
        cfg = Config(
            data_dir=tmp_path / "data",
            workspaces_root=tmp_path / "ws",
            gateway="caddy",
        )
        store = Store(cfg.db_path)
        app = create_app(
            cfg, store=store, orchestrator=FakeOrch(), keepalive=FakeKeepalive()
        )
        r = TestClient(app).get("/api/gateway")
        assert r.json() == {"gateway": "caddy"}

    def test_frontend_guard_wired(self, tmp_path):
        _, _, c = _client(tmp_path)
        js = c.get("/static/app.js").text
        # 카운트 채널 + 임계 + 게이트웨이 조건 + named-window 재사용
        assert "agentcli_tab_presence" in js
        assert "MAX_HELD_TABS" in js
        assert "/api/gateway" in js
        assert '"agentcli-" + post_id' in js
        # v1.16.0: Web Locks 는 secure context 전용이라 LAN http(주 운용)
        # 에서 무동작 — ping/pong 샘플링 카운트로 복귀. 열기는 사람 속도의
        # 버튼 클릭이라 샘플링으로 충분.
        assert "navigator.locks" not in js
        assert "agentcli-conn-slot-" not in js

    def test_open_gate_sampling_window_is_small(self, tmp_path):
        """열기 게이트 샘플링 창(HELD_SAMPLE_MS)은 열기 클릭 직후
        about:blank 창이 대기하는 시간이라 재열기 체감 지연에 직결된다 —
        실측상 방 페이지 로드는 87ms 인데 옛 300ms 창이 "blank 이후"
        지연의 ~77% 였다(v1.22.2 축소). pong 은 같은 브라우저 내 전달이라
        유휴 탭이 ~5ms 에 답하므로 작은 창이면 충분하고, 큰 블로킹 값으로
        되돌아가면 재열기 지연 회귀. 창은 상수로 setTimeout 에 배선돼야
        하드코딩된 큰 값이 우회하지 못한다."""
        import re

        _, _, c = _client(tmp_path)
        js = c.get("/static/app.js").text
        m = re.search(r"HELD_SAMPLE_MS\s*=\s*(\d+)", js)
        assert m, "HELD_SAMPLE_MS 상수 부재 — 샘플링 창이 하드코딩됨?"
        window_ms = int(m.group(1))
        assert window_ms <= 150, (
            f"열기 게이트 샘플링 창 {window_ms}ms — 너무 큼(블로킹 재열기 회귀)"
        )
        # 샘플러가 상수를 실제로 사용하는지(하드코딩 큰 값 차단)
        assert "}, HELD_SAMPLE_MS);" in js

    def test_open_navigates_new_tab_directly_not_blank(self, tmp_path):
        """새 탭을 빈 창(window.open(""))으로 먼저 열고 나중에 navigate
        하면 그 about:blank→실URL 전환이 재열기를 ~1초 굼뜨게 한다
        (v1.22.3 실측 — 직접 URL·현재 탭 열기는 빠른데 빈-창→navigate 만
        느림). 게이트·POST 를 먼저 끝내고 완성된 URL 로 바로 window.open
        해야 한다(await 뒤에도 transient activation 으로 팝업 허용). 빈-창
        패턴 재발 방지."""
        _, _, c = _client(tmp_path)
        js = c.get("/static/app.js").text
        # 빈 창 먼저 열기 금지 — 이 패턴이 재열기 굼뜸의 주범
        assert 'window.open("", ' not in js
        # 완성된 URL 로 직접 열어야(named target 재사용은 유지)
        assert 'window.open(url, "agentcli-" + post_id)' in js


class TestSingletonLock:
    def test_first_acquires_second_refused(self, tmp_path):
        data = tmp_path / "data"
        fd = acquire_singleton_lock(data)
        try:
            assert isinstance(fd, int)
            # a second board on the same data_dir is refused (None)
            assert acquire_singleton_lock(data) is None
        finally:
            os.close(fd)

    def test_pidfile_records_holder(self, tmp_path):
        data = tmp_path / "data"
        fd = acquire_singleton_lock(data)
        try:
            assert (data / "board.lock").read_text().strip() == str(os.getpid())
        finally:
            os.close(fd)

    def test_release_allows_reacquire(self, tmp_path):
        # closing the fd drops the flock → a fresh board can take it (e.g. the
        # previous process exited; the kernel released the lock on its behalf)
        data = tmp_path / "data"
        fd = acquire_singleton_lock(data)
        os.close(fd)
        fd2 = acquire_singleton_lock(data)
        try:
            assert isinstance(fd2, int)
        finally:
            os.close(fd2)

    def test_separate_data_dirs_dont_conflict(self, tmp_path):
        # the guard is per-data_dir — two boards on DIFFERENT dirs both start
        fd_a = acquire_singleton_lock(tmp_path / "a")
        fd_b = acquire_singleton_lock(tmp_path / "b")
        try:
            assert isinstance(fd_a, int) and isinstance(fd_b, int)
        finally:
            os.close(fd_a)
            os.close(fd_b)
