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
        self, *, already_up=False, port=50001, session_id="NEWSID", token="TOK"
    ):
        self.already_up = already_up
        self.port = port
        self.session_id = session_id
        self.token = token
        self.spawns = 0
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

    # router-like surface
    def ensure_route(self, post_id, port):
        self.routes[post_id] = port


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
