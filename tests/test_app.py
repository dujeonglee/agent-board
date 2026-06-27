"""Board HTTP API + UI wiring (DESIGN §3/§10).

create_app builds the real Store but accepts injected orchestrator/keepalive so
the create/list/delete/force-active endpoints are tested without spawning real
agent-cli instances.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_board.app import create_app
from agent_board.config import Config
from agent_board.store import Store


class FakeOrch:
    def __init__(self):
        self.opened = []

    async def open(self, post_id):
        self.opened.append(post_id)
        return f"/s/{post_id}/"


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

    def test_create_writes_directive(self, tmp_path):
        cfg, _, c = _client(tmp_path)
        pid = c.post(
            "/api/posts", json={"topic": "t", "directive": "항상 한국어로"}
        ).json()["post_id"]
        d = cfg.workspace_for(pid) / ".agent-cli" / "DIRECTIVE.md"
        assert d.read_text(encoding="utf-8") == "항상 한국어로"

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

    def test_caddy_gateway_does_not_mount_proxy_catchall(self, tmp_path):
        # Caddy owns /s/<id> routing → the board must not mount the proxy.
        assert self._proxy_paths(tmp_path, "caddy") == []

    def test_board_proxy_gateway_mounts_catchall(self, tmp_path):
        assert self._proxy_paths(tmp_path, "board-proxy")  # non-empty


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
        assert "dot.busy" in c.get("/static/style.css").text
