"""Live board events — push status/roster changes to the browser over SSE
instead of the frontend polling ``/api/posts`` (DESIGN §7 Phase 2).

A background scanner samples each post's cheap on-disk *signature* every
``interval`` seconds — ``status.json`` mtime (busy/awaiting/viewers), the
``history.jsonl`` mtime (last query), and pid liveness — and when a post's
signature changes it recomputes that one row and broadcasts it to every
connected EventSource client. The pid-liveness term ensures an instance that
dies WITHOUT cleaning up its ``status.json`` (e.g. SIGKILL) still flips the row
to "off". No periodic full-list poll: the SSE stream carries a heartbeat so a
half-open connection is detected client-side and reconnected.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_board import instances


class LiveEvents:
    """Owns the SSE subscriber set + the mtime-scan loop. ``view_fn`` maps a
    post to its API row dict (``_post_view``), injected to avoid importing the
    app layer here."""

    def __init__(
        self,
        config,
        store,
        view_fn,
        *,
        on_death=None,
        on_sched_requests=None,
        interval: float = 1.0,
    ):
        self._config = config
        self._store = store
        self._view_fn = view_fn
        # on_death(post_id): called once when a post's instance transitions
        # alive→dead (self-reap / crash). Injected so LiveEvents stays free of
        # routing — the app wires it to the gateway's route removal, which lets
        # Caddy fall through to the board's revive handler on the next hit.
        self._on_death = on_death
        # on_sched_requests(post): the workspace's ⏰ schedule-requests.jsonl
        # changed — the app wires it to the file-contract apply + scheduler
        # rearm (docs/schedule-design.md §7). Piggybacks on this scanner so the
        # contract needs NO polling loop of its own. Runs on the executor
        # thread (like the rest of _scan).
        self._on_sched_requests = on_sched_requests
        self._interval = interval
        self._subscribers: set[asyncio.Queue] = set()
        self._sigs: dict[str, tuple] = {}

    # ── subscriptions ───────────────────────────────────────
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _broadcast(self, msg: dict) -> None:
        # unbounded queues → put_nowait never blocks; called from the loop thread
        for q in self._subscribers:
            q.put_nowait(msg)

    # ── change detection ────────────────────────────────────
    def _sig(self, post) -> tuple:
        """A post's cheap change signature: (status.json mtime, history.jsonl
        mtime, pid-alive, ⏰ schedule-requests mtime). Any field flip ⇒ the row
        may have changed."""
        ws = self._config.workspace_for(post.post_id)

        def _mt(p: Path):
            try:
                return p.stat().st_mtime
            except OSError:
                return None

        req_m = _mt(Path(ws) / ".agent-cli" / "schedule-requests.jsonl")
        sid = post.session_id
        if not sid:
            return (None, None, False, req_m)
        sdir = Path(ws) / ".agent-cli" / "sessions" / sid
        info = instances.read_web_json(ws, sid)
        pid = info.get("pid") if info else None
        alive = bool(pid and instances.pid_alive(pid))
        return (_mt(sdir / "status.json"), _mt(sdir / "history.jsonl"), alive, req_m)

    def _scan(self) -> list[dict]:
        """Sync (runs in an executor): diff current signatures vs the last scan,
        return the SSE messages to broadcast."""
        posts = self._store.list_posts()
        events: list[dict] = []
        seen: set[str] = set()
        for post in posts:
            seen.add(post.post_id)
            sig = self._sig(post)
            prev = self._sigs.get(post.post_id)
            if prev != sig:
                # alive→dead edge (prev[2] was True, now False): the instance
                # self-reaped → drop its gateway route. Only on a real edge, and
                # never on a post's first observation (prev is None).
                if self._on_death and prev is not None and prev[2] and not sig[2]:
                    self._on_death(post.post_id)
                # ⏰ 요청파일 변경 edge — view 계산 전에 계약을 반영해 이번
                # post_update 가 이미 새 스케줄 요약을 싣도록 한다.
                if self._on_sched_requests and prev is not None and prev[3] != sig[3]:
                    try:
                        self._on_sched_requests(post)
                    except Exception:
                        pass  # 계약 오류가 스캐너를 죽이면 안 됨
                self._sigs[post.post_id] = sig
                events.append({"type": "post_update", "post": self._view_fn(post)})
        for gone in set(self._sigs) - seen:
            del self._sigs[gone]
            events.append({"type": "post_removed", "post_id": gone})
        return events

    def _prime(self) -> None:
        """Seed the baseline WITHOUT emitting — a client gets the initial state
        from its own ``load()`` on connect; the scanner only pushes DELTAS.

        ⏰ catch-up: requests appended while the board was DOWN have no future
        mtime edge, so apply the contract once at startup (offset-idempotent —
        already-consumed lines are skipped)."""
        for post in self._store.list_posts():
            sig = self._sig(post)
            if self._on_sched_requests and sig[3] is not None:
                try:
                    self._on_sched_requests(post)
                except Exception:
                    pass
            self._sigs[post.post_id] = sig

    async def run(self) -> None:
        """The scan loop. Cancelled on app shutdown."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._prime)
        while True:
            await asyncio.sleep(self._interval)
            try:
                events = await loop.run_in_executor(None, self._scan)
            except Exception:
                events = []  # a scan hiccup must never kill the loop
            for ev in events:
                self._broadcast(ev)
