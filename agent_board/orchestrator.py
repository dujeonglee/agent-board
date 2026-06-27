"""Spawn-or-attach orchestration (DESIGN §5).

``open(post_id)`` ensures the post's agent-cli instance is running and routed,
then returns the URL to send the browser to. A per-post async lock collapses
concurrent opens into a single spawn.

The instance/route side-effects are behind a small ``backend`` object so the
control flow is testable without real processes. ``RealBackend`` (wired in the
app) delegates to ``instances`` + the active ``Router``.
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

    def __init__(self, router):
        self._router = router

    def current_port(self, workspace, session_id) -> int | None:
        if not session_id:
            return None
        info = instances.read_web_json(workspace, session_id)
        if info and instances.alive(info):
            return info.get("port")
        return None

    def pick_free_port(self) -> int:
        return instances.pick_free_port(50000, 60000)

    def spawn_and_wait(self, config: Config, post: Post, *, port: int, token: str):
        proc = instances.spawn(config, post, port=port, token=token)
        workspace = config.workspace_for(post.post_id)
        sid = instances.await_ready(workspace, proc.pid, port)
        return port, sid

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
        """Ensure the instance is up + routed; return its board URL.
        Raises ``KeyError`` for an unknown post."""
        if self.store.get(post_id) is None:
            raise KeyError(post_id)
        async with self._lock(post_id):
            post = self.store.get(post_id)  # re-read inside the lock
            workspace = self.config.workspace_for(post_id)
            port = self.backend.current_port(workspace, post.session_id)
            if port is None:
                free = self.backend.pick_free_port()
                token = secrets.token_urlsafe(16)
                loop = asyncio.get_event_loop()
                port, sid = await loop.run_in_executor(
                    None,
                    lambda: self.backend.spawn_and_wait(
                        self.config, post, port=free, token=token
                    ),
                )
                if sid is None:
                    raise RuntimeError(f"instance for {post_id} did not become ready")
                if post.session_id is None:  # first open → persist new session
                    self.store.set_session_id(post_id, sid)
            self.backend.ensure_route(post_id, port)
            self.store.touch_opened(post_id)
        return f"/s/{post_id}/"
