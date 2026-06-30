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

    def remove_route(self, post_id: str) -> None:
        self._router.remove_route(post_id)

    def live_state(self, post: Post) -> dict:
        """``{status, viewers, ...}`` for the model-change gate (see sessions)."""
        from agent_board import sessions

        return sessions.live_state(
            self._config.workspace_for(post.post_id), post.session_id
        )

    def stop_instance(self, post: Post) -> bool:
        return instances.stop_instance(
            self._config.workspace_for(post.post_id), post.session_id
        )


class Orchestrator:
    def __init__(self, config: Config, store: Store, *, backend):
        self.config = config
        self.store = store
        self.backend = backend
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, post_id: str) -> asyncio.Lock:
        return self._locks.setdefault(post_id, asyncio.Lock())

    async def _ensure_up(self, post: Post) -> tuple[int, str]:
        """Spawn-or-attach the instance + register its route; return
        ``(port, token)``. The CALLER must hold the post lock. Persists the
        session id on first spawn. Shared by ``open`` and the force-active
        respawn in ``change_model``."""
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
                raise RuntimeError(f"instance for {post.post_id} did not become ready")
            if post.session_id is None:  # first open → persist new session
                self.store.set_session_id(post.post_id, sid)
        else:
            port, token = info["port"], info["token"]
        self.backend.ensure_route(post.post_id, port)
        return port, token

    async def open(self, post_id: str) -> str:
        """Ensure the instance is up + routed; return its board URL (with token).
        Raises ``KeyError`` for an unknown post."""
        if self.store.get(post_id) is None:
            raise KeyError(post_id)
        async with self._lock(post_id):
            post = self.store.get(post_id)  # re-read inside the lock
            _, token = await self._ensure_up(post)
            self.store.touch_opened(post_id)
        return f"/s/{post_id}/?token={token}"

    async def change_model(self, post_id: str, model_id: str | None) -> dict:
        """Change a post's model — allowed only when NOBODY is watching: the
        instance is down (``idle``), or up-and-idle (``running``) with 0 human
        viewers. ``working`` (busy) or watched → refused (``{ok: False,
        reason}``). On success the model is persisted and the instance is
        stopped so the next open spawns with it (kill → DEAD); a force-active
        post is brought straight back up on the new model to honour its
        keep-alive. ``KeyError`` for an unknown post."""
        if self.store.get(post_id) is None:
            raise KeyError(post_id)
        model_id = model_id or None
        async with self._lock(post_id):
            post = self.store.get(post_id)
            if (post.model_id or None) == model_id:
                return {"ok": True, "changed": False, "reason": "unchanged"}
            state = self.backend.live_state(post)
            status = state["status"]
            # the force-active keep-alive holds one viewer of its own → exclude it
            humans = state.get("viewers", 0) - (1 if post.force_active else 0)
            if status == "working":
                return {"ok": False, "reason": "busy"}
            if status == "running" and humans > 0:
                return {"ok": False, "reason": "viewers"}
            # allowed — persist, then re-read so spawns use the new model
            self.store.set_model(post_id, model_id)
            post = self.store.get(post_id)
            if status != "idle":  # instance is up → stop it
                self.backend.stop_instance(post)
                self.backend.remove_route(post_id)
            if post.force_active:
                await self._await_dead(post)  # avoid attaching to the dying one
                await self._ensure_up(post)  # respawn on the new model
            return {"ok": True, "changed": True}

    async def _await_dead(self, post: Post, *, timeout: float = 5.0) -> None:
        """Poll until the instance is no longer alive (``backend.info`` is None),
        so a fresh spawn doesn't attach to the still-dying old process."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if (
                await loop.run_in_executor(None, lambda: self.backend.info(post))
                is None
            ):
                return
            await asyncio.sleep(0.1)
