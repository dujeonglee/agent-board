# Changelog

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
