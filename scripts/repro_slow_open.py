#!/usr/bin/env python3
"""agent-board 재열기 느림/크래시(부하) 재현 하니스.

로컬의 window.open blank→navigate 느림은 v1.22.3 에서 고쳤다. 이건 그와
별개인 **서버 부하** 상황(회사) 재현용 — "재열기 15초+·가끔 안 열림(크래시)".

두 모드:
  kill   — 프록시 keep-alive 풀에 stale 연결을 만든 뒤 인스턴스를 kill 해,
           board `_proxy` 의 초기 `send()` 가 ConnectError 가 아닌
           RemoteProtocolError/ReadError(둘 다 좁은 catch 를 빠져나감)로 터져
           **run_asgi 500 + traceback** 이 board stderr 에 찍히는 걸 재현.
  stress — CPU 를 포화시켜, board 의 **동기 health**(`info()`→`httpx.get`,
           executor 없이 async 루프 안)가 이벤트 루프를 막아 재열기·다른
           요청이 함께 stall 되는 cascade 를 측정.

★ board 는 stderr 가 보이게 실행할 것:
      agent-board 2>&1 | tee ~/agentboard.log
  그리고 --board-log ~/agentboard.log 를 넘기면 kill 후 traceback 을 자동 추출.

★ kill 모드는 대상 post 의 인스턴스를 kill 한다(board 가 다음 open 에 revive).
  진행 중 작업이 있는 방 말고 **스크래치 post 를 --post 로 지정**할 것.

사용 예:
  python3 scripts/repro_slow_open.py --board http://127.0.0.1:51966 --list
  python3 scripts/repro_slow_open.py --post <id> --board-log ~/agentboard.log kill
  python3 scripts/repro_slow_open.py --post <id> stress
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


def http(url: str, method: str = "GET", timeout: float = 10.0):
    """(status|None, elapsed_s, body_bytes). None status = 전송 예외(연결 끊김 등)."""
    req = urllib.request.Request(url, method=method)
    t0 = time.monotonic()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, time.monotonic() - t0, r.read()
    except urllib.error.HTTPError as e:
        return e.code, time.monotonic() - t0, e.read()
    except Exception as e:  # noqa: BLE001 - 전송 실패 전부 None 으로
        return None, time.monotonic() - t0, str(e).encode()


def get_posts(board: str) -> list[dict]:
    st, _, body = http(f"{board}/api/posts")
    if st != 200:
        sys.exit(f"/api/posts 실패({st}) — board 가 {board} 에 떠 있나요?")
    return json.loads(body)


def ensure_open(board: str, post_id: str) -> str:
    st, _, body = http(f"{board}/api/posts/{post_id}/open", method="POST", timeout=30)
    if st != 200:
        sys.exit(f"/open 실패({st}): {body[:200]!r}")
    return json.loads(body).get("url", "")


def instance_pid(post_id: str) -> int | None:
    """board 가 spawn 한 agent-cli web 프로세스(--base-path /s/<id>)의 pid."""
    out = subprocess.run(
        ["pgrep", "-f", f"base-path /s/{post_id}"],
        capture_output=True,
        text=True,
    )
    pids = [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    return pids[0] if pids else None


def grep_last_traceback(path: str) -> None:
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except OSError as e:
        print(f"  board 로그 못 읽음({path}): {e}")
        return
    idx = txt.rfind("Traceback (most recent call last)")
    if idx == -1:
        print(f"  {path} 에 Traceback 없음 — 크래시 미발생이거나 stderr 미캡처.")
        print("  board 를 `agent-board 2>&1 | tee ~/agentboard.log` 로 띄웠는지 확인.")
        return
    print("  ═══ board stderr 마지막 traceback ═══")
    # 마지막 traceback 을 예외 라인(들여쓰기 없는 ...Error/Exception: )까지
    # 전부 출력 — 프레임워크 프레임보다 **밑의 router.py 프레임 + 예외 타입**
    # 이 핵심이라 잘리면 안 된다(상한 200 줄).
    lines = txt[idx:].splitlines()
    for j, line in enumerate(lines[:200]):
        print("  " + line)
        if (
            j > 0
            and line
            and not line[0].isspace()
            and ("Error" in line or "Exception" in line)
        ):
            break


def mode_kill(board: str, post_id: str, rounds: int, board_log: str | None) -> None:
    print(f"[kill] post={post_id} rounds={rounds}")
    print(
        "  주의: 이 방의 인스턴스를 kill 합니다(진행 중 작업 소실). 스크래치 방 권장.\n"
    )
    saw_crash = False
    for i in range(rounds):
        ensure_open(board, post_id)  # up/revive
        time.sleep(0.4)
        pid = instance_pid(post_id)
        if pid is None:
            print(f"  [{i}] 인스턴스 pid 못 찾음 — 재시도")
            time.sleep(1.0)
            continue
        # 1) 프록시 경유 요청으로 board→instance keep-alive 풀 연결 생성
        for _ in range(3):
            http(f"{board}/s/{post_id}/static/app.js")
        # 2) 인스턴스 강제 종료 → 풀 연결이 stale(반열림) 상태가 됨
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # 3) 곧바로 프록시 요청 폭탄 → stale 풀 재사용 → send() 예외 → 500(크래시)
        codes = []
        for _ in range(25):
            st, _, _ = http(f"{board}/s/{post_id}/static/app.js", timeout=5)
            codes.append(st)
        n500 = sum(1 for c in codes if c == 500)
        n502 = sum(1 for c in codes if c == 502)  # 좁은 catch 가 잡은 정상 강등
        nnone = sum(1 for c in codes if c is None)
        print(
            f"  [{i}] kill {pid} → 응답 500={n500} 502={n502} "
            f"연결끊김={nnone} 기타={len(codes) - n500 - n502 - nnone}"
        )
        if n500:
            saw_crash = True
            print("      ↑ 500 = 미처리 ASGI 예외(run_asgi 크래시) 재현됨")
        time.sleep(0.6)
    print()
    if saw_crash:
        print("결과: 크래시(500) 재현됨. board stderr 의 traceback 을 확보하세요.")
    else:
        print(
            "결과: 500 미관측. 라운드를 늘리거나(-n), 부하(다른 터미널 stress)와 "
            "함께 돌리면 stale-풀 재사용 창이 넓어져 더 잘 잡힙니다."
        )
    if board_log:
        print()
        grep_last_traceback(board_log)
    else:
        print("\n(--board-log 를 주면 traceback 을 자동 추출합니다.)")


def start_stress(dur: int):
    ncpu = os.cpu_count() or 4
    if shutil.which("stress-ng"):
        print(f"  stress-ng --cpu {ncpu} -t {dur}s")
        return subprocess.Popen(["stress-ng", "--cpu", str(ncpu), "-t", f"{dur}s"])
    print(f"  (stress-ng 없음 → python busy-loop × {ncpu} 폴백)")
    return [
        subprocess.Popen([sys.executable, "-c", "while True: pass"])
        for _ in range(ncpu)
    ]


def stop_stress(proc) -> None:
    for p in proc if isinstance(proc, list) else [proc]:
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass


def mode_stress(board: str, post_id: str, dur: int) -> None:
    print(f"[stress] post={post_id} dur={dur}s\n")
    ensure_open(board, post_id)

    def sample(label: str) -> None:
        # /open → 동기 health(httpx.get) 경유. /api/posts → health 없음(대조).
        _, o, _ = http(f"{board}/api/posts/{post_id}/open", method="POST", timeout=30)
        _, p, _ = http(f"{board}/api/posts", timeout=30)
        print(f"  [{label:5}] /open {o * 1000:6.0f}ms   /api/posts {p * 1000:6.0f}ms")

    print("── baseline (부하 전) ──")
    for _ in range(5):
        sample("idle")
        time.sleep(0.3)

    print("── CPU 포화 중 ──")
    proc = start_stress(dur)
    time.sleep(1.0)
    for _ in range(8):
        sample("load")
        time.sleep(0.3)
    stop_stress(proc)

    print(
        "\n해석: load 에서 /open 이 크게 뛰면 동기 health 가 이벤트 루프를 막는 것."
        "\n      /api/posts(health 없음)까지 같이 뛰면 다른 open 의 health 가 루프를"
        "\n      점유해 board 전역이 stall 되는 cascade 근거."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", nargs="?", choices=["kill", "stress"], help="재현 모드")
    ap.add_argument("--board", default="http://127.0.0.1:51966", help="board base URL")
    ap.add_argument("--post", help="대상 post_id (미지정 시 --list 로 확인)")
    ap.add_argument("--list", action="store_true", help="게시글 목록만 출력")
    ap.add_argument("-n", "--rounds", type=int, default=8, help="kill 라운드 수")
    ap.add_argument("--dur", type=int, default=20, help="stress 지속(초)")
    ap.add_argument(
        "--board-log", help="board stderr 로그 경로(kill 후 traceback 추출)"
    )
    args = ap.parse_args()

    if args.list or (not args.mode):
        posts = get_posts(args.board)
        if not posts:
            sys.exit("게시글이 없습니다 — board 에 스크래치 방을 하나 만드세요.")
        print("post_id                            status    topic")
        for p in posts:
            print(f"  {p['post_id']}  {p.get('status', '?'):8}  {p.get('topic', '')}")
        if not args.mode:
            print("\n모드를 지정하세요: kill | stress  (--post <id> 함께)")
        return

    if not args.post:
        sys.exit("--post <id> 를 지정하세요 (--list 로 확인).")

    if args.mode == "kill":
        mode_kill(args.board, args.post, args.rounds, args.board_log)
    else:
        mode_stress(args.board, args.post, args.dur)


if __name__ == "__main__":
    main()
