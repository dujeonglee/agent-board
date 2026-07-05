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
