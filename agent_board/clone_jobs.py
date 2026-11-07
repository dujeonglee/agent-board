"""복제 작업 상태 (v1.42.0) — ``post_id → CloneJob`` 하나의 사전, 락으로 보호.

복제(`clone.clone_paths`)는 ``POST /api/posts`` 가 끝까지 기다리지 않고 백그라운드
작업으로 돈다. 진행은 이 메모리 상태를 ``GET /api/posts/{id}/clone`` 이 읽는다
(모달 250 ms·카드 1 s 폴링). SSE 에는 싣지 않는다 — 라이브 스캔은 상태 파일
mtime 기반이라 복제 진행을 못 본다.

프로세스 메모리뿐이다: 보드가 재시작하면 작업은 사라지고 반쪽 방이 남는다(복구
로직 없음 — docs/DESIGN.md §4). 끝난 작업(``done``·``failed``)은 :data:`KEEP_S` 동안
남겨 두어 모달이 실패 사유를 읽을 시간을 준다.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

#: 끝난 작업을 남겨 두는 시간(초) — 10분.
KEEP_S = 600.0


@dataclass
class CloneJob:
    state: str = "running"  # "running" | "done" | "failed"
    bytes_done: int = 0
    bytes_total: int = 0
    files_done: int = 0
    files_total: int = 0
    current: str | None = None
    error: str | None = None
    finished_at: float | None = None

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "files_done": self.files_done,
            "files_total": self.files_total,
            "current": self.current,
            "error": self.error,
        }


class CloneJobs:
    """작업 사전. ``clock`` 은 테스트가 만료를 당겨 보게 갈아 끼운다."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._jobs: dict[str, CloneJob] = {}
        self._lock = threading.Lock()

    def start(self, post_id: str) -> CloneJob:
        with self._lock:
            job = CloneJob()
            self._jobs[post_id] = job
            return job

    def plan(self, post_id: str, files_total: int, bytes_total: int) -> None:
        with self._lock:
            job = self._jobs.get(post_id)
            if job is not None:
                job.files_total, job.bytes_total = files_total, bytes_total

    def progress(
        self, post_id: str, files_done: int, bytes_done: int, current: str | None
    ) -> None:
        with self._lock:
            job = self._jobs.get(post_id)
            if job is not None:
                job.files_done, job.bytes_done = files_done, bytes_done
                if current is not None:
                    job.current = current

    def finish(self, post_id: str, *, error: str | None = None) -> None:
        with self._lock:
            job = self._jobs.get(post_id)
            if job is None:
                return
            job.state = "failed" if error else "done"
            job.error = error
            job.current = None
            job.finished_at = self.clock()

    def get(self, post_id: str) -> dict | None:
        """작업 스냅샷 — 없거나 보관 시간이 지났으면 None."""
        with self._lock:
            self._prune()
            job = self._jobs.get(post_id)
            return job.snapshot() if job is not None else None

    def running(self, post_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(post_id)
            return job is not None and job.state == "running"

    def view(self, post_id: str) -> dict | None:
        """목록 행의 ``cloning`` — 진행 중일 때만 스냅샷, 아니면 None."""
        with self._lock:
            job = self._jobs.get(post_id)
            if job is None or job.state != "running":
                return None
            return job.snapshot()

    def _prune(self) -> None:
        now = self.clock()
        gone = [
            pid
            for pid, j in self._jobs.items()
            if j.finished_at is not None and now - j.finished_at > KEEP_S
        ]
        for pid in gone:
            del self._jobs[pid]
