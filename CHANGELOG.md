# Changelog

## [1.5.1] - 2026-07-03

### Fixed

- **강제 재시작 시 프록시 ASGI 오류(`RemoteProtocolError`) 제거** — 방을 🔄 재실행하면
  인스턴스가 SSE 스트림(`/api/stream`)이 열린 채 죽는다. 상류가 깨끗한 chunked EOF 없이
  소켓을 닫아 board 프록시의 httpx 읽기가 `RemoteProtocolError`("peer closed connection
  without sending complete message body")를 던지고, 그게 `StreamingResponse` 밖으로
  전파돼 board 로그에 ASGI 500 트레이스백이 찍혔다. `BoardProxyRouter` 가 상류 본문을
  `_relay_body` 제너레이터로 중계하며 `httpx.TransportError`(=RemoteProtocolError 상위)를
  삼켜 **스트림을 깨끗이 종료**한다 → 브라우저 EventSource 가 재연결해 재시작된 인스턴스에
  안착. 재시작뿐 아니라 idle-reap·모델 변경 등 모든 상류 중도 종료에 적용.

## [1.5.0] - 2026-07-03

### Added

- **🔄 방별 재실행 버튼** — 게시글 카드의 🔄 로 그 방의 agent-cli 인스턴스를 **강제
  재시작**(stop → respawn)한다. 새로 설치한 agent-cli 를 반영할 때 쓴다. **게이트 없이
  언제든** 동작(응답 중·접속자 있어도 — `change_model` 과 달리 막지 않음). 죽이기 전
  web.json 의 **토큰을 재사용**해 재spawn 하므로 이미 열려 있던 뷰어는 URL(`?token=`)이
  그대로 유효해 자동 재연결되고, `--resume` 로 **같은 세션**을 이어간다. 버튼은 인스턴스가
  떠 있을 때만 노출(꺼진 글은 "열기"가 이미 새로 spawn). `POST /api/posts/{id}/restart`,
  `Orchestrator.restart`(per-post lock, `_ensure_up(reuse_token=…)` 신설).

## [1.4.0] - 2026-06-30

### Added

- **게시물별 모델 변경 (드롭다운)** — 게시글 행의 모델 드롭다운으로 언제든 모델을 바꾼다.
  여러 명이 쓰는 환경이라 **아무도 보고 있지 않을 때만** 허용: 인스턴스가 꺼져 있거나(idle),
  떠 있어도 접속자 0 + 응답 중 아님(running). 응답 중(busy)이거나 접속자가 있으면 드롭다운이
  **비활성**(`POST /api/posts/{id}/model`, 거부 시 409 `busy`/`viewers`). 변경 시 꺼진 글은
  다음 열기에 새 모델로, 떠 있는 글은 멈춰(kill→DEAD) 다음 열기에 적용 — **force-active 글은
  즉시 새 모델로 재시작**(유지 약속 보존). 세션은 `--resume` 으로 이어져 맥락 유지 + 모델만 교체.
- **실시간 접속자 수 표시** — 목록 카드에 👁 N (라이브 SSE 구독자, 5초 폴링). force-active 의
  보드 keepalive 1명은 제외한 실제 사람 수. agent-cli ≥ 4.17.11(`/api/health.viewers`) 필요.

### Changed

- `GET /api/posts` 각 글에 `viewers`·`model_changeable` 추가. `Store.set_model`,
  `orchestrator.change_model`(per-post lock + apply 직전 게이트 재확인) 신설.

## [1.3.1] - 2026-06-29

### Changed

- **spawn 된 agent-cli 인스턴스 출력을 콘솔 대신 파일로** — 인스턴스의 startup 배너
  (UI/Token/Session)가 보드 콘솔로 inherit 돼 시끄럽던 것을 `<workspace>/.agent-cli/
  instance.log` 로 리다이렉트(stderr 병합). 디버깅용으로 파일엔 남김.

## [1.3.0] - 2026-06-29

### Added

- **멈춘 인스턴스 자동 revive** — idle-reap 으로 종료된 글의 옛 `/s/<id>` URL 로 다시
  접속하면, 보드 프록시가 인스턴스를 **재spawn(resume) 후 프록시**한다. 라우트가 없으면
  접속 시 revive, 죽은 포트면(ConnectError) 한 번 revive+재시도(GET/HEAD). "접속하면
  resume" 설계가 보드 목록뿐 아니라 **직접 URL 접속**에도 적용됨. (caddy 모드는 Caddy 가
  502 반환.)

### Fixed

- **멈춘 인스턴스 옛 URL 접속 시 unhandled exception(500)** — board-proxy 가 죽은
  포트로 프록시하다 `ConnectError` 가 안 잡혀 ASGI 예외가 났음. 이제 revive 또는 깨끗한
  502/503(예외 아님).

## [1.2.3] - 2026-06-29

### Fixed

- **열기 500 — 사내 HTTP 프록시가 loopback 호출을 가로채던 문제** — `HTTP_PROXY` 가
  설정된 사내 환경에서 보드의 내부 httpx 호출(health 체크·프록시·keepalive·Caddy admin)이
  `127.0.0.1` 요청까지 프록시로 보내, 프록시가 "Access Denied" 회신 → health 실패 →
  `await_ready` 타임아웃 → `/open` 500. 모든 내부 httpx 클라이언트에 `trust_env=False`
  추가 → loopback 은 프록시를 우회. (임시 회피: `export NO_PROXY=127.0.0.1,localhost`)

## [1.2.2] - 2026-06-29

### Fixed

- **열기 500 — spawn 된 agent-cli 가 "Resume? [y/N]" 프롬프트로 멈춤** — 인스턴스를
  spawn 할 때 stdin 을 안 끊어, 자식이 보드 터미널 stdin 을 물려받아 TTY 로 인식 →
  워크스페이스에 기존 세션이 있으면 agent-cli 가 resume 여부를 대화형으로 물으며 블록 →
  서버 미기동 → web.json 미생성 → `await_ready` 타임아웃 → `/open` 500. spawn 에
  `stdin=DEVNULL` 추가(비대화형) → agent-cli 가 프롬프트 없이 결정적으로 기동.

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
