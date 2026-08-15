# 설계 — 게시글별 예약 실행 (Schedules)

> **상태**: 📋 설계 (승인 대기, 코드 미착수)
> **핸드오프**: 이 문서만 읽으면 다른 세션이 이어서 완결 가능하도록 작성.
> **저장소**: agent-board(주) + agent-cli(에이전트 채널 계약, 소규모) — 두 repo.
> **작성**: 2026-08-13

---

## 0. 확정된 결정 (사용자)

| 결정 | 선택 |
|---|---|
| 스케줄러 위치 | **board 내부 asyncio 스케줄러** (live_events/keepalive 와 동형). board 상시성은 OS 레이어(launchd/systemd)가 보장 — §8 |
| 스케줄 문법 | **cron식** (`분 시 일 월 요일`, 예 `0 9 * * 1`). 최소 파서 자체 구현(새 의존성 0). UI 는 사람이 읽는 라벨 병기 |
| 에이전트 등록 채널 | **파일 기반 계약** (cli↔board 디커플 유지 — cli 는 board 를 모름) |
| 인스턴스 수명 | **발화 시 spawn-then-inject** — 평소 꺼둠, 발화 때 orchestrator.open()(spawn-or-attach, `--resume`) 후 주입 |
| 액션 종류 | **프롬프트 주입만** (kind=inject 단일). "셸 돌려줘"도 프롬프트로 → 에이전트가 가드(confine/confirm)를 거쳐 실행. board 직접 shell 없음 |
| 놓친 실행 | **자동실행 없음 — 사용자에게 질문**. board 가 놓친 발화를 감지하면 `missed` 상태로 두고 UI 에서 "지금 실행 / 건너뛰기"를 물음. 여러 주기 놓쳐도 질문 1건으로 접음 |

## 1. 요구사항 (원문 리뷰)

1. 게시글(post)별 예약 — 각 스케줄은 post 에 종속. ✅ `post_id` FK
2. 사용자 추가분 / 에이전트 추가분 구분 표시. ✅ `source` 컬럼 + UI 배지(👤/🤖)
3. 사용자·에이전트 모두 필요 시 삭제 가능. ✅ UI 삭제 + 파일 계약의 delete op
4. 예: "매주 월요일 주간 보고 작성" 을 자동으로 LLM 에 주입. ✅ kind=inject
5. 발화 시점에 agent-cli 가 꺼져 있으면 **재시작 후 주입**. ✅ spawn-then-inject (기존 open 기계 재사용)
6. ~~또는 크론 있으면 상시 유지~~ → 기각(자원 소모), spawn-then-inject 채택

## 2. 데이터 모델 (board SQLite — `board.db`)

```sql
CREATE TABLE schedules (
  schedule_id   TEXT PRIMARY KEY,          -- uuid4 hex
  post_id       TEXT NOT NULL,             -- 게시글 종속 (posts.post_id)
  source        TEXT NOT NULL,             -- 'user' | 'agent'
  cron          TEXT NOT NULL,             -- '분 시 일 월 요일' (5필드)
  prompt        TEXT NOT NULL,             -- 주입할 사용자 요청 텍스트
  label         TEXT NOT NULL DEFAULT '',  -- UI 표시용 짧은 이름 (예 "주간 보고")
  enabled       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  last_fired_at TEXT,                      -- 마지막 정상 발화 (exactly-once 가드)
  missed_at     TEXT                       -- NULL 아니면 "놓친 발화 대기 중" (질문 상태)
);
CREATE INDEX idx_schedules_post ON schedules(post_id);
```

- **user_version 2 마이그레이션**: 기동 시 `PRAGMA user_version` 확인 → 1 이면 CREATE TABLE + version 2. 기존 DB 무손상.
- post 삭제 시 그 post 의 schedules 도 삭제 (delete_post 경로에 추가).
- `missed_at`: 스캐너가 "이전 발화 시각 < 지금인데 last_fired_at 이 그 이전" 을 감지하면 스탬프. 여러 주기 놓쳐도 **덮어쓰기 1건**(질문 접기). 사용자가 실행/건너뛰기를 고르면 NULL 로 클리어.

## 3. cron 파서 (`agent_board/cron.py`, 순수 모듈 — 의존 0)

- 5필드: `분 시 일 월 요일`. 지원 토큰: `*`, 숫자, 리스트(`1,3,5`), 범위(`1-5`), 스텝(`*/15`). 요일 0=일(및 7=일 alias).
- 핵심 API:
  - `parse(expr) -> CronSpec` (잘못된 식은 ValueError — API 400 으로)
  - `next_fire(spec, after: datetime) -> datetime` — after 이후 첫 발화 시각(분 단위 반복으로 탐색, 최대 366일 룩어헤드 후 실패 시 에러)
  - `describe(expr) -> str` — "매주 월 09:00" 류 한국어 라벨(대표 패턴만; 일반식은 원문 표기)
- 타임존: **서버 로컬시간**(naive). 문서화.
- TDD: 대표식(매일/매주/매월/스텝/리스트)·경계(월말·연말)·잘못된 식 거부.

## 4. 스케줄러 (`agent_board/scheduler.py`)

asyncio 백그라운드 태스크(app lifespan start/stop). **폴링이 아니라 sleep-until-next + rearm** (사용자 결정 — 리스트 변경 시 재무장):

```
루프:
  next = min(next_fire(s, after=now) for enabled s)   # 스케줄 0개면 rearm 까지 대기
  timeout = min(next − now, 300s)                     # ★300s 상한 = 시스템 sleep/시계점프 안전망
  await asyncio.wait_for(rearm_event.wait(), timeout) # rearm 오면 즉시, 아니면 발화 시각(or heartbeat)에 기상
  rearm_event.clear()
  for s in enabled schedules:                         # 기상 시 due 정산
    prev = 직전 발화 시각(now 기준 역산)
    if last_fired_at >= prev: continue                # 이미 실행됨 (exactly-once)
    if now − prev > MISS_THRESHOLD(120s):             # 제때 못 깼음(board 다운·시스템 sleep)
        s.missed_at = prev                            # 자동실행 금지 — §5 질문 플로우
        live_events push
    else:
        fire(s)                                       # 정시 발화 (지연 ≈0)

fire(s):
  1. url = await orchestrator.open(s.post_id)      # spawn-or-attach (--resume) — 요구 5
  2. 인스턴스 /api/input 에 POST (loopback, trust-local):
       {kind:"chat", content: s.prompt}            # 닉네임 "⏰schedule" 로 귀속
  3. last_fired_at = now, missed_at = NULL
  4. 실패(스폰 실패·주입 4xx) 시: missed_at 스탬프(사용자 질문으로 강등) + 로그
```

- **rearm_event.set() 호출원**: ① API 변이(add/delete/toggle/run-now/dismiss) ② **live_events 스캐너가 에이전트 요청파일 변경을 감지·반영했을 때**(§7). 파일 계약은 이벤트로 도착하지 않으므로 이미 1s 로 도는 기존 스캐너에 stat 1개만 추가 — 새 폴링 루프 0개. (부수효과: 에이전트 `schedule` 도구 ack 이 1~2s 내 확정 — 도구가 state 파일을 짧게 폴링해 결과 반환 가능.)
- **300s 상한이 필요한 이유**: `asyncio.sleep`/`wait_for` 타이머는 시스템 sleep 동안 정지 — Mac 이 자고 깨면 늦게 발화. 상한 덕에 깨어난 뒤 ≤5분 내 재평가되고, 자는 동안 지난 발화는 MISS_THRESHOLD 초과 → missed 질문으로 자연 수습. 5분당 1회 기상은 사실상 0 비용.
- **exactly-once**: `last_fired_at` 비교가 유일 가드 — 단일 태스크라 락 불필요; fire 는 순차. (뮤테이션 테스트 대상)
- **주입 귀속**: `/api/input` 의 `nickname` 경로를 이용해 트랜스크립트·팀뷰에 **⏰schedule** 로 표시(누가 시켰는지 명확).
- busy 인스턴스: `/api/input` chat 은 큐잉되므로(턴 경계 주입) 그대로 안전 — 게이트 불필요.

## 5. 놓친 실행 — 질문 플로우 (자동실행 없음)

- `missed_at != NULL` 인 스케줄은 board UI post 카드에 배지: **"⏰ 놓친 예약: <label> — [지금 실행] [건너뛰기]"**.
- `POST /api/schedules/{id}/run-now` → fire(s) (missed 클리어 겸용, 수동 즉시실행 버튼과 공용).
- `POST /api/schedules/{id}/dismiss-missed` → `missed_at=NULL` 만.
- live_events SSE 로 배지 실시간 반영(기존 post_update 채널에 스케줄 요약 동봉).

## 6. board API + UI

| 메서드 | 경로 | 동작 |
|---|---|---|
| GET | `/api/posts/{id}/schedules` | 그 글의 스케줄 목록 (source·label·cron·describe·next_fire·missed 포함) |
| POST | `/api/posts/{id}/schedules` | `{cron, prompt, label?}` → source='user' 로 추가 (cron 검증, 400) |
| DELETE | `/api/schedules/{sid}` | 삭제 (user/agent 소스 무관 — 사용자는 뭐든 삭제 가능) |
| POST | `/api/schedules/{sid}/toggle` | enabled 토글 |
| POST | `/api/schedules/{sid}/run-now` | 즉시 발화 (missed 클리어 겸용) |
| POST | `/api/schedules/{sid}/dismiss-missed` | 놓친 발화 건너뛰기 |

UI (post 카드 확장 또는 ⏰ 드로어):
- 목록 행: `👤/🤖 배지 · label · "매주 월 09:00" · 다음 발화 시각 · [토글][지금 실행][삭제]`
- 추가 폼: label / cron 입력(+ 대표 프리셋 버튼이 cron 식을 채워줌 — 문법은 cron 단일) / prompt textarea
- 놓친 발화 배지 (§5)
- 에이전트 추가 개수 **캡(기본 5/post)** — 초과 시 파일 계약에서 거부(§7).

## 7. 에이전트 채널 — 파일 기반 계약 (cli↔board 디커플)

에이전트는 board 를 모른다. **워크스페이스 파일**로 요청하고, board 스캐너가 반영한다. (web.json/status.json 과 같은 on-disk 통합 계약의 역방향 확장.)

### 7.1 파일
- **요청**: `<workspace>/.agent-cli/schedule-requests.jsonl` — 에이전트가 append.
  ```jsonl
  {"op":"add","cron":"0 9 * * 1","prompt":"주간 보고를 작성해줘","label":"주간 보고","req_id":"r1"}
  {"op":"delete","schedule_id":"...","req_id":"r2"}
  {"op":"list","req_id":"r3"}
  ```
- **상태(회신)**: `<workspace>/.agent-cli/schedule-state.json` — board 가 원자적(mkstemp+replace)으로 재기록. 현재 스케줄 전체 + 각 req_id 의 처리 결과(`ok`/`error:...`). 에이전트는 이 파일을 read_file 로 확인.
- board 스캐너가 요청 파일을 tick 마다 확인(mtime 게이트), 처리한 라인 수를 오프셋으로 기억(state 에 `consumed` 기록) — 재처리 없음. 처리 후 요청 파일은 truncate 하지 않고 오프셋 방식(단순·안전) 또는 처리분 제거 중 택1 — **구현 시 오프셋 방식 채택**.

### 7.2 에이전트가 계약을 "아는" 방법 (agent-cli 측 소규모 변경)
- cli 는 board 를 모르므로, **일반화된 opt-in**: board 가 spawn 시 env `AGENT_CLI_SCHEDULER=1` 을 준다("외부 스케줄러가 이 워크스페이스를 본다"는 신호일 뿐, board 특정 아님).
- env 가 있으면 agent-cli 가:
  - **`schedule` 도구 등록** (add/delete/list — 위 jsonl 을 검증해 써 주고 state 파일을 읽어 결과 반환). 모델이 손으로 jsonl 을 쓰는 것보다 구조화 도구가 오류에 강함.
  - 시스템 프롬프트에 짧은 안내 섹션("예약이 필요하면 schedule 도구 사용").
- env 없으면(일반 CLI 사용) 도구 미등록 — 표면 무변화. **cli 는 "어떤 오케스트레이터가 있다"만 알고 board 는 모름** — 경계 유지.
- source='agent' 는 board 가 파일 경로 출처로 판정(요청 파일 경유 = agent).

### 7.3 안전
- 에이전트 추가 캡(기본 5/post, board 설정) — 초과 add 는 state 에 error 로 회신.
- 에이전트는 **자기 post 의 스케줄만** 보고 지울 수 있음(요청 파일이 그 워크스페이스에 있으므로 자연 격리).
- 사용자는 UI 에서 agent 스케줄 포함 전부 열람·삭제 가능(요구 3). UI 에 🤖 배지로 구분(요구 2).

## 8. board 상시성 (OS 레이어 — 이 기능의 배포 전제)

board 내부 스케줄러는 board 가 떠 있어야 발화한다. 상시성은 OS 가 보장:
- **macOS(현 환경)**: `deploy/com.agentboard.plist` (launchd, `KeepAlive=true`, `RunAtLoad=true`) 신규 제공 + README 설치 절차.
- **Linux(prod)**: 기존 `deploy/agent-board.service` (systemd `Restart=always`) 문서에 스케줄 전제 명시.
- board 가 꺼져 있던 동안의 발화는 §5 질문 플로우로 수습(자동 몰아치기 없음 — 사용자 결정).

## 9. 테스트 계획

- **cron.py**: 파서/next_fire/describe 유닛 (경계·거부 포함).
- **scheduler**: 가짜 clock + 가짜 orchestrator/inject 로 — 정시 발화 1회(exactly-once)·중복 방지·**rearm**(대기 중 add 가 sleep 을 깨워 더 이른 발화를 잡음 / delete 가 예정 발화를 취소)·MISS_THRESHOLD 경계(초과=missed 스탬프+자동실행 안 함이 핵심 회귀, 이내=발화)·fire 실패 시 missed 강등. 뮤테이션: last_fired_at 가드 제거 시 중복 발화 테스트 실패해야.
- **API/UI**: CRUD·toggle·run-now·dismiss·cron 400·post 삭제 cascade.
- **파일 계약**: add/delete/list 반영·오프셋 재처리 없음·캡 초과 error·state 원자 기록. (cli 쪽 schedule 도구는 cli repo 에서 유닛 + env 게이트 테스트.)
- **cli**: env 없으면 도구 미등록(표면 무변화) 계약 테스트.

## 10. 버전·릴리스

- **agent-board 1.26.0 (MINOR)** — 테이블 마이그레이션 + 스케줄러 + API/UI + 파일 계약 소비 + launchd/systemd 문서.
- **agent-cli 8.9.0 (MINOR)** — env 게이트 `schedule` 도구 + 프롬프트 섹션 (env 없으면 무변화).
- 각 repo 단일 커밋 + wheel release (기존 절차).

## 11. 구현 순서 (핸드오프 체크리스트)

- [ ] board: cron.py (TDD)
- [ ] board: schedules 테이블 + store CRUD + user_version 마이그레이션
- [ ] board: scheduler.py (fire/missed/exactly-once) + lifespan 배선
- [ ] board: API 6종 + post 삭제 cascade + live_events 스케줄 요약
- [ ] board: UI (목록·추가 폼·배지·놓친발화 질문)
- [ ] board: 파일 계약 소비(요청 오프셋·state 원자 기록·캡)
- [ ] board: launchd plist + README/DESIGN 문서 + CHANGELOG → 1.26.0 릴리스
- [ ] cli: env 게이트 schedule 도구 + 프롬프트 섹션 + 계약 테스트 → 8.9.0 릴리스
- [ ] board: spawn 시 `AGENT_CLI_SCHEDULER=1` env 추가 (cli 릴리스 후)
