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
  instances.py    agent-cli spawn · free-port · web.json 읽기 · health · pid 생존
  router.py       게이트웨이 연동 (Router 인터페이스 + CaddyRouter / BoardProxyRouter)
  keepalive.py    force-active = 인스턴스에 SSE 연결 유지 (asyncio task)
  sessions.py     agent-cli 세션 파일 읽기 (last_query, status)  ← on-disk 통합 계약
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
  directive       TEXT,                  -- DIRECTIVE.md 내용 (선택)
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
| GET | `/api/posts` | 글 목록 — 각 글에 `last_query`·`status`·`force_active` 유도 포함 |
| POST | `/api/posts` | 새 글 `{topic, directive?}` → 생성 (workspace 자동) |
| DELETE | `/api/posts/{id}` | 글 삭제 + 그 글의 워크스페이스 디렉토리 삭제(보드 소유) |
| POST | `/api/posts/{id}/open` | spawn-or-attach → 라우트 등록 → `{url}` 반환(프론트 리다이렉트) |
| POST | `/api/posts/{id}/force_active` | `{enabled}` 토글 |

## 4. 새 글 생성 흐름 (`POST /api/posts {topic, directive?}`)
1. `post_id` 발급 → `posts` 행 삽입(`session_id=NULL`).
2. `mkdir <WORKSPACES_ROOT>/<post_id>/` (보드 소유 빈 워크스페이스). 실패 시 row 롤백.
3. `directive` 있으면 `<ws>/.agent-cli/DIRECTIVE.md` 기록.
   *(agent-cli 세션은 아직 안 만듦 — 첫 open 에 생성.)*
- **사용자 경로 입력 없음** → 임의 경로/traversal 위험 0, 공유 충돌 0(post_id 파생).

## 5. ★ 글 열기 흐름 (`POST /api/posts/{id}/open`) — 핵심
**per-post 비동기 lock** 으로 동시 클릭 시 spawn 1회만.
```
post = store.get(id)
async with lock[id]:
  info = instances.read_web_json(post)         # 떠 있나?
  if info and instances.alive(info):           # pid 살아있음 + /api/health OK
     port = info.port
  else:
     port = pick_free_port()
     pid  = instances.spawn(post, port)        # 아래 6
     info = instances.await_ready(post, port, pid)   # web.json + health 대기
     if post.session_id is None:               # 첫 spawn = 새 세션
        store.set_session_id(id, info.session_id)
     port = info.port
  router.ensure_route(post_id=id, port=port)   # 게이트웨이에 /s/<id>→127.0.0.1:port 등록
return {"url": f"/s/{id}/?token=...or trust-local"}   # 프론트가 location 이동
```

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

## 7. status · last_query 유도 (`sessions.py`) — on-disk 통합 계약
agent-cli 를 수정 안 하므로 **세션 파일을 직접 읽음**(통합 지점):
- **status**: `web.json` 있음 + pid 생존 + `/api/health` 200 → `running`, else `idle`.
- **last_query**: `<workspace>/.agent-cli/sessions/<sid>/history.jsonl` 마지막 user query
  레코드(`role=user, kind=query`)의 text. (세션 없으면 "—".)
- ⚠️ agent-cli on-disk 포맷에 의존 → README 에 "통합 계약" 으로 명시(버전 호환 주의).

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
- **CaddyRouter (prod)**: Caddy admin API(`:2019`)에 `/s/<id>→127.0.0.1:port`(handle_path strip)
  POST/DELETE.
- 라우트 등록: open 성공 직후. 해제: 인스턴스 idle 종료 감지 시(또는 lazy — 다음 open 에 갱신).

## 10. 보드 UI (static)
- 글 목록: 카드마다 **주제 · 마지막 쿼리 · 상태(running/idle) 점 · force-active 체크박스**.
- **새 글** 폼: 주제 / DIRECTIVE.md(textarea, 선택). (workspace 입력 없음 — 자동 생성)
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

## 14. v2+ (범위 밖, 설계 여지만)
- 글별 권한/소유권, 기존 CLI 세션 노출, 검색/필터, presence, 글별 모델, 멀티호스트.
