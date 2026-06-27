"""Spawn-or-attach orchestration (DESIGN §5).

``open(post_id)`` ensures the post's agent-cli instance is running and routed,
then returns the URL to send the browser to — **with the instance token**:
``/s/<post_id>/?token=<token>``. agent-cli's frontend requires a token in the
URL to connect (its client-side gate), so even with ``--trust-local`` (which
only relaxes the server side) the browser still needs it. A per-post async lock
collapses concurrent opens into a single spawn.

Instance/route side-effects are behind a small ``backend`` so the control flow
is testable without real processes. ``RealBackend`` delegates to ``instances``
+ the active ``Router``.
"""

from __future__ import annotations

import asyncio
import secrets

from agent_board import instances
from agent_board.config import Config
from agent_board.models import Post
from agent_board.store import Store


class RealBackend:
    """Production backend: ``instances`` + a ``Router``."""

    def __init__(self, config: Config, router):
        self._config = config
        self._router = router

    def info(self, post: Post) -> dict | None:
        """``{port, token}`` if the instance is up, else None."""
        if not post.session_id:
            return None
        ws = self._config.workspace_for(post.post_id)
        wj = instances.read_web_json(ws, post.session_id)
        if wj and instances.alive(wj):
            return {"port": wj.get("port"), "token": wj.get("token")}
        return None

    def pick_free_port(self) -> int:
        return instances.pick_free_port(self._config.port_min, self._config.port_max)

    def spawn_and_wait(self, post: Post, *, port: int, token: str) -> str | None:
        proc = instances.spawn(self._config, post, port=port, token=token)
        ws = self._config.workspace_for(post.post_id)
        return instances.await_ready(ws, proc.pid, port)

    def ensure_route(self, post_id: str, port: int) -> None:
        self._router.ensure_route(post_id, port)


class Orchestrator:
    def __init__(self, config: Config, store: Store, *, backend):
        self.config = config
        self.store = store
        self.backend = backend
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, post_id: str) -> asyncio.Lock:
        return self._locks.setdefault(post_id, asyncio.Lock())

    async def open(self, post_id: str) -> str:
        """Ensure the instance is up + routed; return its board URL (with token).
        Raises ``KeyError`` for an unknown post."""
        if self.store.get(post_id) is None:
            raise KeyError(post_id)
        async with self._lock(post_id):
            post = self.store.get(post_id)  # re-read inside the lock
            info = self.backend.info(post)
            if info is None:  # not up → spawn
                port = self.backend.pick_free_port()
                token = secrets.token_urlsafe(16)
                loop = asyncio.get_event_loop()
                sid = await loop.run_in_executor(
                    None,
                    lambda: self.backend.spawn_and_wait(post, port=port, token=token),
                )
                if sid is None:
                    raise RuntimeError(f"instance for {post_id} did not become ready")
                if post.session_id is None:  # first open → persist new session
                    self.store.set_session_id(post_id, sid)
            else:
                port, token = info["port"], info["token"]
            self.backend.ensure_route(post_id, port)
            self.store.touch_opened(post_id)
        return f"/s/{post_id}/?token={token}"
