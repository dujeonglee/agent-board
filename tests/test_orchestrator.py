"""Spawn-or-attach orchestration (DESIGN §5).

The orchestrator decides: instance already up → reuse; else spawn, await ready,
persist the discovered session_id (first open). A per-post async lock collapses
concurrent opens into one spawn. Instance side-effects are injected (a fake
``instances``-like backend) so the control flow is tested without real
processes.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_board.config import Config
from agent_board.orchestrator import Orchestrator
from agent_board.store import Store


class FakeBackend:
    """Stand-in for instances.*: records spawns, fakes readiness."""

    def __init__(
        self,
        *,
        already_up=False,
        port=50001,
        session_id="NEWSID",
        token="TOK",
        status=None,
        viewers=0,
    ):
        self.already_up = already_up
        self.port = port
        self.session_id = session_id
        self.token = token
        self.spawns = 0
        self.stops = 0
        self.status = status  # override for the change_model gate
        self.viewers = viewers
        self.routes: dict[str, int] = {}

    # instances-like surface
    def info(self, post):
        # {port, token} if up, else None
        if self.already_up and post.session_id:
            return {"port": self.port, "token": self.token}
        return None

    def spawn_and_wait(self, post, *, port, token):
        self.spawns += 1
        self.already_up = True  # instance is now running (info will see it)
        return self.session_id  # discovered session_id

    def pick_free_port(self):
        return self.port

    def live_state(self, post):
        st = self.status or (
            "running" if (self.already_up and post.session_id) else "idle"
        )
        return {"status": st, "awaiting_input": False, "viewers": self.viewers}

    def stop_instance(self, post):
        self.already_up = False  # now down → info() returns None
        self.stops += 1
        return True

    # router-like surface
    def ensure_route(self, post_id, port):
        self.routes[post_id] = port

    def remove_route(self, post_id):
        self.routes.pop(post_id, None)


@pytest.fixture
def setup(tmp_path):
    cfg = Config(data_dir=tmp_path / "data", workspaces_root=tmp_path / "ws")
    store = Store(cfg.db_path)
    yield cfg, store
    store.close()


@pytest.mark.asyncio
async def test_first_open_spawns_and_persists_session_id(setup):
    cfg, store = setup
    be = FakeBackend(already_up=False, session_id="S-FIRST")
    orch = Orchestrator(cfg, store, backend=be)
    post = store.create_post(topic="t")

    url = await orch.open(post.post_id)

    assert be.spawns == 1
    assert store.get(post.post_id).session_id == "S-FIRST"  # persisted
    assert be.routes[post.post_id] == be.port  # route registered
    assert url.startswith(f"/s/{post.post_id}/")
    assert "token=" in url  # agent-cli frontend needs the token in the URL
    assert store.get(post.post_id).last_opened_at  # touched


@pytest.mark.asyncio
async def test_attach_when_already_up_no_spawn(setup):
    cfg, store = setup
    be = FakeBackend(already_up=True)
    orch = Orchestrator(cfg, store, backend=be)
    post = store.create_post(topic="t")
    store.set_session_id(post.post_id, "EXISTING")

    await orch.open(post.post_id)
    assert be.spawns == 0  # reused the running instance


@pytest.mark.asyncio
async def test_second_open_resumes_not_new(setup):
    cfg, store = setup
    be = FakeBackend(already_up=False, session_id="S1")
    orch = Orchestrator(cfg, store, backend=be)
    post = store.create_post(topic="t")

    await orch.open(post.post_id)  # first: new session S1
    be.already_up = False  # reaped between opens
    await orch.open(post.post_id)  # second: spawns again but with resume
    # session_id stays the same (resume, not a new session each time)
    assert store.get(post.post_id).session_id == "S1"


@pytest.mark.asyncio
async def test_concurrent_opens_spawn_once(setup):
    cfg, store = setup
    be = FakeBackend(already_up=False)

    # make spawn slow so both opens overlap inside the lock
    orig = be.spawn_and_wait

    def slow_spawn(*a, **k):
        import time

        time.sleep(0.05)
        return orig(*a, **k)

    be.spawn_and_wait = slow_spawn
    orch = Orchestrator(cfg, store, backend=be)
    post = store.create_post(topic="t")

    await asyncio.gather(orch.open(post.post_id), orch.open(post.post_id))
    assert be.spawns == 1  # per-post lock collapsed the two into one spawn


@pytest.mark.asyncio
async def test_open_missing_post_raises(setup):
    cfg, store = setup
    orch = Orchestrator(cfg, store, backend=FakeBackend())
    with pytest.raises(KeyError):
        await orch.open("nope")


class TestChangeModel:
    """Per-post model change gate: allowed only when nobody is watching
    (down, or up-and-idle with 0 human viewers). On success the model is
    persisted + the instance stopped (kill → DEAD); force-active respawns."""

    @pytest.mark.asyncio
    async def test_dead_stores_model_no_spawn(self, setup):
        cfg, store = setup
        be = FakeBackend(already_up=False)  # status idle (down)
        orch = Orchestrator(cfg, store, backend=be)
        post = store.create_post(topic="t", model_id="old")
        res = await orch.change_model(post.post_id, "new")
        assert res == {"ok": True, "changed": True}
        assert store.get(post.post_id).model_id == "new"
        assert be.spawns == 0 and be.stops == 0  # lazy: next open uses it

    @pytest.mark.asyncio
    async def test_idle_no_viewers_stops_then_dead(self, setup):
        cfg, store = setup
        be = FakeBackend(already_up=True, status="running", viewers=0)
        orch = Orchestrator(cfg, store, backend=be)
        post = store.create_post(topic="t", model_id="old")
        store.set_session_id(post.post_id, "S1")
        be.ensure_route(post.post_id, be.port)
        res = await orch.change_model(post.post_id, "new")
        assert res["changed"] is True
        assert store.get(post.post_id).model_id == "new"
        assert be.stops == 1 and be.spawns == 0  # killed, left DEAD
        assert post.post_id not in be.routes

    @pytest.mark.asyncio
    async def test_rejected_when_busy(self, setup):
        cfg, store = setup
        be = FakeBackend(already_up=True, status="working", viewers=0)
        orch = Orchestrator(cfg, store, backend=be)
        post = store.create_post(topic="t", model_id="old")
        store.set_session_id(post.post_id, "S1")
        res = await orch.change_model(post.post_id, "new")
        assert res == {"ok": False, "reason": "busy"}
        assert store.get(post.post_id).model_id == "old"  # unchanged
        assert be.stops == 0

    @pytest.mark.asyncio
    async def test_rejected_when_viewers_present(self, setup):
        cfg, store = setup
        be = FakeBackend(already_up=True, status="running", viewers=2)
        orch = Orchestrator(cfg, store, backend=be)
        post = store.create_post(topic="t", model_id="old")
        store.set_session_id(post.post_id, "S1")
        res = await orch.change_model(post.post_id, "new")
        assert res == {"ok": False, "reason": "viewers"}
        assert store.get(post.post_id).model_id == "old"

    @pytest.mark.asyncio
    async def test_force_active_excludes_keepalive_and_respawns(self, setup):
        cfg, store = setup
        # only the board's own keep-alive viewer is connected (viewers=1)
        be = FakeBackend(already_up=True, status="running", viewers=1)
        orch = Orchestrator(cfg, store, backend=be)
        post = store.create_post(topic="t", model_id="old")
        store.set_session_id(post.post_id, "S1")
        store.set_force_active(post.post_id, True)
        be.ensure_route(post.post_id, be.port)
        res = await orch.change_model(post.post_id, "new")
        assert res["changed"] is True  # keep-alive viewer excluded → allowed
        assert store.get(post.post_id).model_id == "new"
        assert be.stops == 1 and be.spawns == 1  # killed AND brought back up
        assert be.routes[post.post_id] == be.port  # re-routed

    @pytest.mark.asyncio
    async def test_unchanged_is_noop(self, setup):
        cfg, store = setup
        be = FakeBackend(already_up=True, status="working")  # busy, but no-op wins
        orch = Orchestrator(cfg, store, backend=be)
        post = store.create_post(topic="t", model_id="same")
        res = await orch.change_model(post.post_id, "same")
        assert res == {"ok": True, "changed": False, "reason": "unchanged"}
        assert be.stops == 0
