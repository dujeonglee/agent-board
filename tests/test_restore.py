"""Board-restart state restoration (app.restore_state).

BoardProxyRouter's route map is in-memory, so a restart loses it while detached
instances stay alive — re-register routes for live instances + restore
force-active keepalives, else an already-open browser gets 404 on /s/<id>.
"""

from __future__ import annotations

import json

import pytest

from agent_board import app as appmod
from agent_board.config import Config
from agent_board.store import Store


class FakeRouter:
    def __init__(self):
        self.routes = {}

    def ensure_route(self, post_id, port):
        self.routes[post_id] = port


class FakeKeepalive:
    def __init__(self):
        self.enabled = []

    async def enable(self, post_id):
        self.enabled.append(post_id)


def _web_json(cfg, post, *, pid, port):
    d = cfg.workspace_for(post.post_id) / ".agent-cli" / "sessions" / post.session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "web.json").write_text(json.dumps({"pid": pid, "port": port, "token": "t"}))


@pytest.mark.asyncio
async def test_restore_reregisters_live_routes(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path / "d", workspaces_root=tmp_path / "w")
    store = Store(cfg.db_path)
    live = store.create_post(topic="live")
    store.set_session_id(live.post_id, "S-LIVE")
    _web_json(cfg, store.get(live.post_id), pid=1, port=55001)
    store.create_post(topic="never-opened")  # no session → skipped

    monkeypatch.setattr(appmod.instances, "alive", lambda info: True)
    router, ka = FakeRouter(), FakeKeepalive()
    await appmod.restore_state(cfg, store, router, ka)

    assert router.routes == {live.post_id: 55001}  # only the live one
    assert ka.enabled == []


@pytest.mark.asyncio
async def test_restore_skips_dead_instances(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path / "d", workspaces_root=tmp_path / "w")
    store = Store(cfg.db_path)
    p = store.create_post(topic="dead")
    store.set_session_id(p.post_id, "S")
    _web_json(cfg, store.get(p.post_id), pid=1, port=55002)
    monkeypatch.setattr(appmod.instances, "alive", lambda info: False)
    router = FakeRouter()
    await appmod.restore_state(cfg, store, router, FakeKeepalive())
    assert router.routes == {}  # dead → no route


@pytest.mark.asyncio
async def test_restore_reenables_force_active(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path / "d", workspaces_root=tmp_path / "w")
    store = Store(cfg.db_path)
    p = store.create_post(topic="kept")
    store.set_force_active(p.post_id, True)
    monkeypatch.setattr(appmod.instances, "alive", lambda info: False)
    ka = FakeKeepalive()
    await appmod.restore_state(cfg, store, FakeRouter(), ka)
    assert ka.enabled == [p.post_id]
