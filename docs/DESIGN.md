# agent-board — DESIGN

> REQUIREMENTS.md 의 v1 을 구현하기 위한 설계. 컨트롤 플레인(이 서비스)의 모듈
> 구조 · API · 저장소 · 인스턴스 라이프사이클 · force-active · 게이트웨이 연동.

---

## 0. 핵심 결정 (먼저 읽기)

### 라우팅 데이터 평면 — **v1 = (B) 보드 프록시 (확정)**
`Router` 인터페이스 뒤에 두 구현:
- **`BoardProxyRouter` (v1)**: 보드가 `/s/<id>/*` catch-all 로 받아 httpx 스트리밍으로
  `127.0.0.1:<port>` 에 중계. **무의존 + open→접속 전체를 in-process e2e 테스트 가능**
  (꼼꼼한 TDD 에 유리). SSE 무버퍼 패스스루가 핵심 주의점.
- **`CaddyRouter` (prod 승급)**: Caddy admin API 에 `/s/<id>→port` 동적 등록. TLS·단일포트·
  재시작 견고. v1 검증 후 한 줄 스위치.

→ **보드 본체(store·orchestrator·instances·spawn·web.json)는 A/B 동일** — Router 만 다름.

### 식별자 — post_id vs session_id
- **post_id**: 보드가 글 생성 시 발급(안정적, 라우트 `/s/<post_id>` 에 사용).
- **session_id**: agent-cli 가 **첫 spawn 때** 만드는 세션 id. 생성 시점엔 없음 →
  첫 open 에서 발견해 글에 저장. 이후 open 은 `--resume <session_id>`.

---

## 1. 모듈 구조
```
agent_board/
  app.py          FastAPI 앱 · 라우트 · static 서빙
  config.py       설정(WORKSPACES_ROOT, AGENT_CLI_BIN, IDLE_TIMEOUT, GATEWAY_*)
  models.py       Post 데이터클래스
  store.py        SQLite 글 레지스트리 (CRUD, 단일 board.db)
  orchestrator.py 글 open = spawn-or-attach 조율 (per-post lock)
  instances.py    agent-cli spawn · free-port · web.json/status.json 읽기 · health(폴백) · pid 생존
  router.py       게이트웨이 연동 (Router 인터페이스 + CaddyRouter / BoardProxyRouter)
  keepalive.py    force-active = 인스턴스에 SSE 연결 유지 (asyncio task)
  sessions.py     agent-cli 세션 파일 읽기 (last_query, status)  ← on-disk 통합 계약
  live_events.py  라이브 push — mtime 스캐너 + /api/events SSE (프론트 폴링 제거, §7 Phase 2)
  static/         보드 UI (index.html · app.js · style.css)
```

## 2. 데이터 저장 — SQLite (`<DATA_DIR>/board.db`, stdlib sqlite3, 무의존)

**왜 SQLite (JSON 아님)**: 글은 수십 개라 JSON 도 되지만, **공유 방어(한 workspace=한 글)**
를 `workspace UNIQUE` + 트랜잭션으로 DB 가 보장 — JSON 은 동시 생성 레이스에서 같은
workspace 를 둘이 잡을 수 있음.

```sql
PRAGMA journal_mode=WAL;     -- 동시 읽기/쓰기
PRAGMA user_version=1;       -- 향후 마이그레이션 훅
CREATE TABLE posts (
  post_id         TEXT PRIMARY KEY,      -- 보드 발급 (uuid) — workspace 가 여기서 파생
  topic           TEXT NOT NULL,
  session_id      TEXT UNIQUE,           -- NULL until first open; 한 세션=한 글
  force_active    INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  last_opened_at  TEXT                   -- 목록 최근순 정렬 키
);
CREATE INDEX idx_posts_recent ON posts(last_opened_at DESC, created_at DESC);
```
- **workspace 컬럼 없음**: 항상 `<WORKSPACES_ROOT>/<post_id>/` 로 **파생**(저장 불필요).
  사용자 경로 입력이 없어 공유 방어·경로 정규화·UNIQUE 문제 자체가 소멸.

**설계 규칙 (중요):**
- **ephemeral 미저장**: `port`·`token`·`route`·`status`·`last_query` 는 **DB 에 절대 저장 안 함**
  — `web.json`(port/token/pid) + 세션 파일이 진실. 저장하면 stale 버그.
- **async 접근**: 블로킹 `sqlite3` 호출을 `run_in_executor`(threadpool)로 감싸 이벤트
  루프 안 막음(무의존). (대안 `aiosqlite` = 의존 +1.)
- **생성 원자성**: insert(post 행) → `mkdir <WORKSPACES_ROOT>/<post_id>` 순서, mkdir
  실패 시 row 롤백 — orphan/불일치 방지.
- **재시작 복구**: 영속=post 메타+`force_active`+`session_id`. 파생(재구성)=port/token/status
  (web.json), 라우트(running 인스턴스에서 재등록), keepalive task(`force_active=1`).

## 3. 보드 API
| 메서드 | 경로 | 동작 |
|---|---|---|
| GET | `/` | 보드 UI(글 목록) |
| GET | `/api/posts` | 글 목록 — 각 글에 `last_query`·`status`·`force_active`·`viewers`·`model_changeable` 유도 포함 |
| POST | `/api/posts` | 새 글 `{topic, model_id?}` → 생성 (workspace 자동) |
| DELETE | `/api/posts/{id}` | 글 삭제 + 그 글의 워크스페이스 디렉토리 삭제(보드 소유) |
| POST | `/api/posts/{id}/open` | spawn-or-attach → 라우트 등록 → `{url}` 반환(프론트 리다이렉트) |
| POST | `/api/posts/{id}/restart` | 강제 재시작 = stop + respawn → `{url}` 반환. **게이트 없음**(busy/viewer 무시 — 새로 설치한 agent-cli 반영 목적). **토큰 재사용**(열려 있던 뷰어 자동 재연결) + `--resume` 로 같은 세션 유지 |
| POST | `/api/posts/{id}/force_active` | `{enabled}` 토글 |
| POST | `/api/posts/{id}/model` | `{model_id}` 변경 — 게이트 통과 200, 거부 409(`detail`=`busy`/`viewers`) (§16) |

## 4. 새 글 생성 흐름 (`POST /api/posts {topic, model_id?}`)
1. `post_id` 발급 → `posts` 행 삽입(`session_id=NULL`).
2. `mkdir <WORKSPACES_ROOT>/<post_id>/` (보드 소유 빈 워크스페이스). 실패 시 row 롤백.
   *(agent-cli 세션은 아직 안 만듦 — 첫 open 에 생성.)*
- **사용자 경로 입력 없음** → 임의 경로/traversal 위험 0, 공유 충돌 0(post_id 파생).
- **DIRECTIVE.md 는 보드가 기록하지 않음** — 세션별 지시는 agent-cli 세션 내 Directives
  드로어 또는 전역 `~/.agent-cli/DIRECTIVE.md` 로 관리(중복·열등한 폼 입력 제거, v1.6.0).

## 5. ★ 글 열기 흐름 (`POST /api/posts/{id}/open`) — 핵심
**per-post 비동기 lock** 으로 동시 클릭 시 spawn 1회만.
```
post = store.get(id)
async with lock[id]:
  info = backend.info(post)                    # 떠 있으면 {port, token}, 아니면 None
  if info is None:                             # 없으면 spawn
     port  = backend.pick_free_port()
     token = secrets.token_urlsafe(16)         # 보드가 토큰 생성
     sid   = backend.spawn_and_wait(post, port, token)   # 아래 6
     if post.session_id is None:               # 첫 spawn = 새 세션
        store.set_session_id(id, sid)
  else:
     port, token = info.port, info.token       # attach: web.json 의 토큰
  backend.ensure_route(post_id=id, port=port)
  store.touch_opened(id)
return {"url": f"/s/{id}/?token={token}"}      # 프론트가 location 이동
```
**★ 토큰을 URL 에 실어야 함**: agent-cli 프론트(app.js)는 브라우저 URL 에 `?token=` 이
없으면 연결을 시도조차 안 함(클라이언트측 게이트). `--trust-local` 은 *서버측* 만
풀어주므로 프론트엔 토큰이 필요 → `backend.info`/spawn 이 토큰을 함께 반환.

## 6. spawn 상세 (`instances.spawn`) — session_id 발견이 관건
- **보드가 port·token 을 정해서 넘김**(라우트를 바로 등록할 수 있게).
- 명령:
  ```
  agent-cli web --host 127.0.0.1 --port <port> --token <tok> --no-browser
                --idle-timeout <N> --trust-local --base-path /s/<post_id>
                [--resume <session_id>  # session_id 있을 때만]
  cwd = post.workspace
  ```
- **session_id 발견(첫 spawn, --resume 없을 때)**: agent-cli 가
  `<workspace>/.agent-cli/sessions/<sid>/web.json` 을 쓰면, **그 중 `pid == 방금 띄운 child pid`**
  인 파일을 찾아 `session_id` 획득. (stdout 파싱보다 견고 — pid 매칭)
- `await_ready`: web.json 생성 + `/api/health` 200 까지 짧게 폴링(타임아웃).
- **`start_new_session=True`**: 인스턴스를 보드와 독립된 세션/프로세스 그룹으로 spawn —
  idle 자가종료·보드 재시작 후 재attach·보드 Ctrl+C 시 비신호(좀비 없음). 명시적 종료는
  `stop_instance`(web.json 의 pid 로 SIGTERM)로, 삭제 시 rmtree 전에 호출(고아 방지).

## 7. status · last_query 유도 (`sessions.py`) — on-disk 통합 계약
agent-cli 를 수정 안 하므로 **세션 파일을 직접 읽음**(통합 지점):
- **status (3단계)**: `web.json` 없음/pid 죽음 → `idle`(꺼짐). 살아있으면 라이브 상태를
  **`status.json` 사이드카**(`{busy, awaiting_input, viewers}`, agent-cli ≥ 4.27.0 가 변화마다
  기록)에서 읽어 `busy` → `working`(LLM 응답 중) / `running`(대기). **파일 read 라 HTTP 폴링 없음.**
  `status.json` 없는 옛 인스턴스(< 4.27.0)는 `GET /api/health` 로 **폴백**(같은 `{busy,…}` 형태).
  둘 다 실패 → `idle`. (`instances.read_status_json` → 없으면 `health_info`.)
- **last_query**: `<workspace>/.agent-cli/sessions/<sid>/history.jsonl` 마지막 user query
  레코드(`role=user, kind=query`)의 text. (세션 없으면 None.)
- ⚠️ agent-cli on-disk(`status.json`/`web.json`/history) + /api/health 폴백 포맷에 의존
  → "통합 계약"(버전 호환 주의).
- ✅ **Phase 2 (라이브 push, `live_events.py`)**: 프론트의 5초 `/api/posts` 폴링을 제거하고
  **보드 SSE push** 로 대체. 백그라운드 스캐너(`LiveEvents.run`)가 1초마다 각 글의 **on-disk
  시그니처**(`status.json` mtime + `history.jsonl` mtime + pid 생존)를 표집하고, 시그니처가
  바뀐 **그 행만** 재계산해 `/api/events` 구독자에게 `post_update`/`post_removed` 로 broadcast
  한다. pid-생존 항은 `status.json` 정리 없이 죽은(SIGKILL) 인스턴스도 `off` 로 뒤집는다.
  주기 full-list 폴링 없음 — SSE 스트림이 **15초 heartbeat(`ping`)** 를 실어보내고, 프론트
  워치독이 30초 무수신 시 half-open(sleep/wake·불안정 망)으로 보고 강제 재연결한다. 매
  (재)연결마다 프론트가 한 번 full `load()` 하므로 공백 구간 이벤트도 유실 없음. 스캔은
  executor(별 스레드)에서 돌려 이벤트 루프를 막지 않고, 스캔 예외는 삼켜 루프를 유지한다.

## 8. force-active (`keepalive.py`)
- 토글 ON: 그 인스턴스가 없으면 먼저 open(spawn) 후, **컨트롤 플레인이 그 인스턴스의
  `/api/stream` 에 SSE 연결을 하나 유지**(loopback + trust-local 이라 토큰 불필요).
  → agent-cli `has_live_connections()` 가 항상 true → `--idle-timeout` 회수 안 됨.
- 토글 OFF: 그 SSE 연결을 닫음 → 뷰어 0 되면 정상 idle 회수.
- 인스턴스 크래시/재기동 시 keep-alive task 가 재연결(또는 재spawn).
- post 당 task 1개. 보드 재시작 시 `force_active=1` 인 글들 task 복원.

## 9. 라우팅 (`router.py`)
- 인터페이스: `ensure_route(post_id, port)` / `remove_route(post_id)` (+ 보드프록시는 조회만).
- **BoardProxyRouter (v1)**: 보드 앱에 catch-all 라우트 `GET/POST /s/{post_id}/{path:path}` —
  in-memory `{post_id→port}` 로 upstream 찾고 httpx 로 중계. **SSE 무버퍼**: `client.stream()`
  + async generator 로 청크 즉시 yield → `StreamingResponse`. 헤더·쿼리·바디(업로드) 전달,
  클라 disconnect 시 upstream 닫기. prefix `/s/<id>` 는 path 에서 제거하고 `/api/...` 로 전달
  (agent-cli 는 `--base-path` 로 *프론트가 emit 하는* URL 만 prefix; 서버 routes 는 `/api`).
- **CaddyRouter (prod, 구현됨)**: Caddy admin API(`:2019`)에 `/s/<id>` 라우트를 등록
  (`@id=agentboard-<id>` 로 멱등 replace, routes/0 삽입). handle=[**authentication**,
  rewrite strip, reverse_proxy]. **★ 인증을 각 동적 라우트에 직접 임베드** → 삽입 순서와
  무관하게 인증 우회 불가(`AGENT_BOARD_CADDY_BASIC_AUTH="user:bcrypt"`). `mount()` no-op.
  단위테스트는 admin 호출만 검증 → 실제 Caddy 인증은 배포 후 curl 로 실측(deploy/Caddyfile).
  `AGENT_BOARD_GATEWAY=caddy|board-proxy` 로 선택.
- 라우트 등록: open 성공 직후. 해제: 인스턴스 idle 종료 감지 시(또는 lazy — 다음 open 에 갱신).

**SSE 무버퍼 — 클라 disconnect 시 upstream 닫기**: `body()` generator 의 `finally` 에서
`aclose()` 하면 disconnect 로 generator 가 취소될 때 그 await 가 미완료될 수 있어 upstream
이 누수(agent-cli 가 떠난 viewer 를 계속 카운트 → 로스터 누적). `StreamingResponse(
background=BackgroundTask(upstream.aclose))` 로 응답 종료(취소 포함) 후 확실히 닫음.

## 10. 보드 UI (static)
- 글 목록: 카드마다 **주제 · 마지막 쿼리 · 상태(🔵응답중/🟢대기/⚪꺼짐) · 접속자 수(👁) ·
  모델 드롭다운(게이트 비활성, §16) · force-active 체크박스**.
- **새 글** 폼: 주제 / 모델(선택). (workspace 입력 없음 — 자동 생성; DIRECTIVE.md 입력 제거)
- 클릭 → `POST …/open` → 반환 url 로 이동(`location.href`).
- 삭제 버튼(확인). force-active 체크 → `POST …/force_active`.

## 11. 설정 (`config.py`)
```
WORKSPACES_ROOT  = /srv/agent-board/workspaces   # (c) 새 워크스페이스 base
AGENT_CLI_BIN    = agent-cli                       # 스폰 바이너리
IDLE_TIMEOUT     = 300                             # spawn 시 --idle-timeout 값
GATEWAY          = caddy | traefik | board-proxy
CADDY_ADMIN      = http://127.0.0.1:2019
PORT_RANGE       = (50000, 60000)                  # 인스턴스 포트 할당
```

## 12. 동시성 · 엣지 (구현 시 가드)
- **per-post spawn lock**: 동시 open → spawn 1회.
- **stale web.json**: pid 죽었는데 파일 남음 → alive() 가 걸러 재spawn.
- **포트 충돌**: pick_free_port + 재시도.
- **첫 open 실패(spawn 타임아웃)**: 에러 반환, session_id 미저장(다음 시도 재spawn).
- **삭제 안전**: 워크스페이스가 항상 `WORKSPACES_ROOT/<post_id>` 라 rmtree 전 그 prefix
  하위인지 재확인(보드 소유만 삭제).

## 13. 단계 (구현 순서 제안)
1. store + models + config (SQLite CRUD, TDD)
2. instances (spawn·pick_port·web.json·health·pid — 순수/통합 테스트)
3. sessions (last_query·status 유도 — fixture 세션으로 TDD)
4. orchestrator (spawn-or-attach + lock)
5. router (**BoardProxyRouter (v1)** — SSE 무버퍼 프록시 + in-process e2e 테스트; CaddyRouter 는 prod 승급)
6. keepalive (force-active)
7. app + UI (API 라우트 + 정적 프론트)

## 14. agent-cli 버전 의존 (통합 계약)
보드가 의존하는 agent-cli 기능/수정 (인스턴스가 이 버전 이상이어야 깨끗이 동작):
- `--idle-timeout`·web.json·`--trust-local`·`--base-path` (v4.13~4.16)
- **v4.17.1** — app.js `pagehide`→`es.close()`: 페이지 이탈/bfcache 시 SSE 해제. 없으면
  뒤로가기→다시열기 마다 유령 viewer 누적 + idle 종료 안 됨.
- **v4.17.2** — `/api/health` 의 `busy` 필드: 3단계 status(응답 중/대기)의 근거. 없으면
  `running` 으로 degrade.
- **v4.17.3** — auto-review 인터럽트 정지(보드와 무관한 일반 버그지만 같이 검증됨).
- **v4.17.5** — `/api/health` 의 `awaiting_input`: ❗ 응답 필요 표시.
- **v4.17.11** — `/api/health` 의 `viewers`(라이브 구독자 수): 접속자 수 표시(👁) +
  게시물별 모델 변경 게이트(§16). 없으면 `viewers`=0 으로 degrade(=항상 변경 가능처럼 보임).

## 15. v2+ (범위 밖, 설계 여지만)
- 글별 권한/소유권, 기존 CLI 세션 노출, 검색/필터, presence, 멀티호스트.
- **프로덕션화(다음 마일스톤)**: CaddyRouter(TLS·재시작 견고) + 게이트웨이 인증 + 배포.

## 16. 게시물별 모델 변경 + 접속자 수 (gate)
게시글 행의 모델 드롭다운으로 모델을 바꾼다. 여러 명이 동시에 쓰는 환경이라, **아무도 보고
있지 않을 때만** 허용한다(보고 있던 사람의 세션을 뺏지 않기 위해).

**게이트** (`orchestrator.change_model`, per-post lock 으로 직렬화):
| 인스턴스 상태 | 변경 | 동작 |
|---|---|---|
| `working`(busy) | ❌ 409 `busy` | LLM 생성 중단 안 함 |
| `running` + human viewers>0 | ❌ 409 `viewers` | 보고 있는 사람 보호 |
| `running` + human viewers=0 | ✅ | model 저장 → `stop_instance`+`remove_route` (kill→DEAD) |
| `idle`(down) | ✅ | model 저장만 → 다음 open 이 `--model` 새 값으로 spawn |

- **human viewers** = `/api/health.viewers`(라이브 SSE 구독자) − (force-active 면 보드 keepalive 1).
  `_post_view` 가 `viewers`(보정값)+`model_changeable` 를 내보내 프론트가 드롭다운을 **비활성**.
- **force-active 예외**: kill 후 DEAD 로 두면 "유지" 약속이 깨지므로, **즉시 새 모델로
  재시작**(`_await_dead`[죽을 때까지 대기 → 죽어가는 인스턴스에 attach 방지] → `_ensure_up`).
- 세션은 `--resume <sid> --model <new>` 로 이어진다(모델은 세션에 안 박힘 — agent-cli 가
  매 실행 시 `--model` 로 해석). 맥락 유지 + 모델만 교체.
- **동시성**: 클릭~서버처리 사이 상태가 바뀔 수 있어 **apply 직전 게이트 재확인**(락 안에서
  live_state 재조회). 거부되면 409 → 프론트가 드롭다운 되돌리고 사유 표시.
- **접속자 수**: 위 `viewers` 를 목록에 `👁 N` 으로 표시(SSE 라이브 push, §7 Phase 2). agent-cli ≥ 4.17.11 필요.
