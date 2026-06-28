# Changelog

## [1.2.1] - 2026-06-29

### Fixed

- **sqlite3 없는 Python 에서 `No module named '_sqlite3'` 로 기동 실패** — `store.py`
  가 `import sqlite3` 를 모듈 최상단에 둬, stdlib sqlite3 확장이 없는 Python(잠금
  서버·최소 빌드)에서 보드가 안 떴음. `agent_board._sqlite` shim(stdlib sqlite3 →
  `pysqlite3-binary` 폴백)을 거치도록 변경 + `pysqlite3-binary` 의존성 추가(x86_64
  Linux). agent-cli 와 동일한 해법.

## [1.2.0] - 2026-06-28

### Added

- **포트 충돌 시 다이나믹 fallback** — `AGENT_BOARD_PORT` 미지정 시 기본 포트가 사용
  중이면 OS 할당 free 포트로 자동 전환(`pick_board_port`) → 두 번째 보드/잔류 프로세스가
  "address already in use" 로 죽지 않음. `AGENT_BOARD_PORT` 명시 시엔 정확히 바인드(충돌
  시 에러).

### Changed

- **기본 포트 0xC0DE → `0xCAFE` (51966)** — agent-cli web 기본(0xC0DE=49374)과 안
  겹치게. (agent-cli 와 짝: cli=0xC0DE, board=0xCAFE.)

## [1.1.1] - 2026-06-28

### Changed

- **기본 포트 8001 → 49374 (`0xC0DE` = "CODE" 🙂)** — 인스턴스 포트 범위(50000~60000)
  보다 아래이고 omlx-server 의 8000 도 피함. `AGENT_BOARD_PORT` 로 여전히 오버라이드 가능.

## [1.1.0] - 2026-06-28

### Added

- **열기 = 새 탭 기본** — 글 "열기" 가 새 탭에서 열려 보드 목록은 그대로 유지됨.
  헤더의 **"현재 페이지에서 열기"** 체크박스를 켜면 기존처럼 현재 탭에서 열림(선택은
  localStorage 로 기억).

### Fixed

- **모바일 반응형** — 좁은 화면에서 새 글 폼·카드가 뭉개지던 것을 `@media` 로 세로
  스택/줄바꿈 처리.

### Changed

- 헤더 "글 하나 = agent-cli 세션" 문구 제거, 주제 placeholder "주제 - 예: 라이브러리 만들기".

## [1.0.0] - 2026-06-28

첫 정식 릴리스. 게시판형 웹 서비스 — **글 하나 = agent-cli 세션 + 워크스페이스 + 주제**.
agent-cli 를 외부 CLI 로만 호출(무수정). 실제 Caddy e2e 로 게이트웨이 경로 검증 완료.

### 글 라이프사이클
- 새 글: 주제 + (선택) DIRECTIVE.md + (선택) 모델 → 워크스페이스 자동 생성(글당, 경로
  입력 없음 → traversal/공유충돌 0).
- 열기: spawn-or-attach(per-post lock, 첫 open=새 세션·이후 `--resume`), `/s/<id>/?token=`.
- idle 자가종료(agent-cli `--idle-timeout`) + 보드 재시작 시 살아있는 인스턴스 재attach.
- force-active("유지"): 접속자 0 이어도 인스턴스 유지(keepalive SSE).
- 삭제: 인스턴스 종료 + 게이트웨이 라우트 해제 + 워크스페이스 삭제.

### 게시글 상태 표시
- 3단계 상태: 🔵 응답 중 / 🟢 대기 / ⚪ 꺼짐 (`/api/health` busy 기반).
- ❗ 응답 필요: ask/confirm 대기(awaiting_input) 강조.
- 주제 · 마지막 쿼리 · 생성일 · 마지막 쿼리 일시 · 모델 태그.

### 라우팅 (게이트웨이)
- `board-proxy`(기본): 보드가 직접 SSE 무버퍼 프록시. 무의존.
- `caddy`(프로덕션): Caddy admin API 동적 라우트, 보드는 데이터 경로 밖(TLS·단일포트).
  각 동적 라우트에 basic_auth 임베드 → **인증 우회 불가**(실제 Caddy e2e 검증).

### 모델 선택
- 글별 모델: `~/.agent-cli/models.json` 레지스트리에서 목록 → `--model` 전달
  (정의/키는 agent-cli 가 해석, 보드에 안 흩어짐).

### 운영
- SQLite 레지스트리(WAL, nullable 마이그레이션). access 로그는 `<DATA_DIR>/board.log`
  (회전 5MB×3), 콘솔엔 startup·에러만.
- 배포물: `deploy/Caddyfile`, `deploy/agent-board.service`(systemd, 리눅스).

### 의존하는 agent-cli (>= 4.17.5 권장)
`--idle-timeout`·web.json·`--trust-local`·`--base-path` (4.13~4.16),
pagehide SSE 해제(4.17.1), `/api/health` busy(4.17.2)·awaiting_input(4.17.5),
auto-review 인터럽트 정지(4.17.3), ask/confirm 프롬프트 sticky(4.17.4).

[1.0.0]: https://github.com/dujeonglee/agent-board/releases/tag/v1.0.0
