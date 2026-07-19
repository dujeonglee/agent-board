# Changelog

## [1.21.0] - 2026-07-19

### Changed

- **복제 UX 를 모달로** — 새 글 폼의 clone 패널·📋 복제 토글을 제거하고,
  각 글 카드에 `📋 복제` 버튼(열기 옆)을 두어 그 글을 원본으로 모달을
  연다. 한 창(native `<dialog>`)에서 새 주제·모델·파일 트리를 다 설정,
  `복제 생성` 으로 진행. 닫기(backdrop/Esc/✕)나 `취소` = 중단. 원본이
  카드로 미리 스코프되므로 원본 선택 드롭다운도 불필요.


## [1.20.2] - 2026-07-19

### Fixed

- clone 트리가 "복사할 파일이 없습니다" 로 오표시되던 케이스 — 정적
  (app.js)만 새로고침되고 board **프로세스가 구버전**(GET /api/posts/
  {id}/tree 미탑재)이면 트리 요청이 404 인데, 이를 빈 디렉토리로
  삼켰다. 이제 404/에러를 빈 워크스페이스와 구분해 "board 프로세스가
  구버전일 수 있습니다 — 재시작 후 다시 시도하세요" 안내. (실제 원인:
  clone 은 서버 재시작이 필요한 신기능.)


## [1.20.1] - 2026-07-19

### Fixed

- clone 패널에서 "체크하세요" 힌트만 뜨고 **선택 창(트리)이 안 보이던**
  혼란 — 원본 글을 아직 안 골랐거나 원본 워크스페이스가 비었을 때
  트리가 빈 채였음. 상태별 안내로: 미선택→"먼저 복제 원본을 선택하세요",
  빈 워크스페이스→"복사할 파일이 없습니다(한 번도 열지 않았거나 빈
  워크스페이스)", 파일 있음→트리. 트리 박스에 테두리+최소 높이.


## [1.20.0] - 2026-07-19

### Added

- **대화방 clone** — 새 글 생성 시 `📋 복제` 로 원본 글 선택 + 워크스페이스
  파일 트리(체크박스, lazy 로드)에서 복사할 파일/폴더 선택. `POST
  /api/posts` 가 `{clone_from, clone_paths}` 수용, `GET /api/posts/{id}/tree`
  신설(board 가 fs 직접 — 인스턴스 미기동도 동작). `.agent-cli/sessions/
  <sid>` 포함 시 세션 remap(새 sid rename + session.jsonl `_meta`
  재작성 + web/status/instance.log 제외 → 첫 open 시 `--resume` 로 대화
  이어받기), 미포함 시 파일만 fresh. 경로 traversal 가드 + 실패 시 방
  롤백. `agent_board/clone.py`(순수 함수, 유닛) + 브라우저 e2e.


## [1.19.0] - 2026-07-19

### Added

- **실브라우저 테스트 (`tests/browser/`, playwright)** — 탭 가드(여러 실
  탭의 BroadcastChannel 연결 카운트→열기 차단)와 에이전트 상태 칩(🤖
  칩·보라 dot·hover 상세) 처럼 실 브라우저 없이는 검증 못 하는 프런트
  계약을 고정. 옵트인 `AGENT_BOARD_BROWSER_TESTS=1`, 그 외엔 루트
  conftest collect_ignore 로 미수집(pytest-asyncio 수집-단계 이벤트루프
  누출 방지 — agent-cli 실측 교훈). 이 저장소는 아직 CI 없어 로컬 전용.


## [1.18.0] - 2026-07-18

### Added

- **LAN 용 Caddy 템플릿 `deploy/Caddyfile.lan`** — 도메인 없이
  `https://<LAN_IP>:8443` + `tls internal`(로컬 CA)로 caddy 게이트웨이
  운용. h2 멀티플렉스로 브라우저 origin 당 6연결 제한이 사라져 방을
  몇 개 열든 고갈이 없고(탭 가드 자동 해제), secure context 확보.
  **두 게이트웨이(board-proxy/caddy)는 모두 공식 지원** — 이 템플릿을
  안 쓰면 기본(board-proxy)으로 동작. 실검증(playwright, LAN https):
  h2 협상·방 스폰·8탭 동시 보유 무고갈·revive 전부 확인.

### Fixed

- **좀비 pid 로 caddy revive 고착** — 인스턴스가 크래시/외부 kill 로
  죽으면 부모(board)가 wait 하지 않아 좀비 잔존, `os.kill(pid,0)` 이
  좀비도 살아있다고 판정 → death edge 미발화 → 죽은 라우트가 502 로
  영구 고착(board-proxy 는 lazy revive 라 무증상). `pid_alive` 가
  좀비를 죽은 것으로 판정 + 자식이면 reap.


## [1.17.0] - 2026-07-18

### Added

- **상주 에이전트 상태 표시** (agent-cli ≥ 7.10.0 의 status.json `agents`
  필드) — 행에 `🤖 작업중/전체` 칩 + hover 에이전트별 상세. main 유휴
  + 에이전트 작업 중이면 새 상태 **🟣 "에이전트 작업 중"**(동일 원형
  dot, board 전용 토큰 `--agent-work` 보라 — "응답 중"과 구분해 main
  은 지금 말 걸어도 됨을 보존). 구버전 인스턴스는 필드 부재 → 자동
  숨김.


## [1.16.0] - 2026-07-17

### Changed

- **탭 가드 카운트를 ping/pong 샘플링(창 300ms)으로 복귀** — v1.15 의
  Web Locks 는 secure context 전용이라 LAN http(주 운용)에서 락 API 가
  노출되지 않아 무동작이었다. 열기는 사람 속도의 버튼 클릭이라 샘플링
  으로 충분(agent-cli v7.7.0 board-경유 운용 전제와 세트). 대시보드
  SSE 슬롯 게이트·`#live-wait` 힌트 제거 — 무조건 연결(v1.14 동작).


## [1.15.0] - 2026-07-17

### Changed

- **탭 가드 카운트를 Web Locks 로** (agent-cli v7.6.0 프로토콜) — ping/pong
  샘플링(150ms 창)의 과소/잔존 집계 레이스 제거. 열기 게이트는
  `locks.query()` 의 `agentcli-conn-slot-*` held 정확값을 읽고, 재사용
  path 판정만 비콘 유지.
- **대시보드도 슬롯 보유** — 자기 /api/events SSE 를 열기 전에 슬롯을
  획득, 없으면 목록 정적 표시 + `#live-wait` 힌트 + 슬롯이 나는 순간
  자동 연결. (없으면 방 5 + 대시보드 = 6 포화 구멍)


## [1.14.1] - 2026-07-17

### Fixed

- 탭 가드 카운트가 agent-cli ≥7.5.0 의 **파킹 탭(held:false pong)** 을
  보유로 오집계하던 것 수리 — SSE 를 안 잡은 탭이 열기 한도를 잡아먹지
  않는다. 재사용 path 판정에는 파킹 탭도 포함(named window 는 파킹
  탭도 재사용).


## [1.14.0] - 2026-07-17

### Added

- **탭 가드** — board-proxy 에서 브라우저 origin 당 6연결(HTTP/1.1) 고갈로
  방 UI 전체가 조용히 멈추던 실사고(agent-cli v7.2.0 confirm-starvation)
  예방. 열기 전 BroadcastChannel(`agentcli_tab_presence`) 로 연결 보유 탭
  카운트(방 탭=agent-cli ≥7.3.0 비콘, 대시보드 탭=자체 pong) → 4개째 경고
  토스트, 5개째 차단. 같은 글 재열기는 named window(`agentcli-<post_id>`)
  로 기존 창 재사용(가드 면제 — pong 의 path 로 식별). `GET /api/gateway`
  신설 — gateway=caddy(h2) 면 가드 자동 해제.

### Fixed

- admin: agent-cli v6/v7 잔여 정리 — structured 컬럼·supports_structured_
  output/supports_strict_schema 체크박스 제거(v7.0.0 에서 필드 삭제),
  wire 드롭다운 기본 체인 표기 md_array→json_fc.


## [1.13.0] - 2026-07-17

### Added

- **⚙ admin 모델 행 wire format 바인딩 드롭다운** — agent-cli v5.19.0 모델별
  wire format 바인딩(models.json entry `wire_format`)을 admin 에서 편집.
  테이블에 wire 컬럼, entry 다이얼로그에 드롭다운 (auto = 필드 미기록 —
  해석 체인 위임). 옵션은 agent-cli `list_names()` lazy import — 자유입력
  금지 (agent-cli 부트가 unknown 이름에 fail-fast 하므로 오타 원천 차단),
  미설치 환경은 auto + 현재값 보존만.

### Fixed

- entry 다이얼로그 저장이 entry 를 재조립하며 손으로 넣은 `wire_format` 을
  조용히 떨구던 클로버 — 명시 필드로 승격해 봉합.
- `agent_board.__version__` 드리프트 동기 (1.10.1 → pyproject 추종).


## [1.12.0] - 2026-07-12

### Added

- **🎨 테마 피커 + agent-cli 공유 디자인 시스템** — agent-cli 웹 UI(v5.5.0)와 동일한
  5개 테마(Amber 기본/Slate/Midnight/Terminal/Light) 토큰과 버튼 체계(변형 4종:
  primary/ghost/danger/icon + UA-기본 차단 베이스)를 이식. localStorage 키
  `agentcli_theme` 를 공유하므로 board 프록시로 여는 방들과 테마가 함께 움직인다.
  admin 페이지도 동일 테마 적용(하드코딩 색 → 토큰). 기존 하드코딩 팔레트
  전면 토큰화 — 게시글 카드·상태 점·토스트·모델 셀렉트 등 전부 테마 추종.

## [1.11.1] - 2026-07-12

### Fixed

- **정적/페이지 응답 no-cache** — plain `StaticFiles`/`FileResponse` 는 `Cache-Control`
  미설정이라 브라우저가 HTML/JS 를 휴리스틱 캐시 → 코드를 교체 재기동해도 열려 있던
  탭엔 옛 UI 가 남음 (v1.11.0 의 ⚙ admin 링크가 안 보이던 실사례). `/`·`/admin`·
  `/static/*` 전부 `no-cache, must-revalidate` 스탬프 (`_NoCacheStaticFiles`,
  agent-cli 동형 — 304 fast path 유지). **이번 한 번만 브라우저 새로고침 필요**,
  이후는 서버 재기동만으로 반영.

## [1.11.0] - 2026-07-12

### Added

- **⚙ admin 페이지 (`/admin`)** — agent-cli `config.json`/`models.json` 편집 표면
  (기존 "board 는 registry 를 읽기만" 경계를 의도적으로 반전 — 사용자 결정).
  - `config.json`: 알려진 4필드(provider/base_url/api_key/default_model) 폼 편집 —
    api_key 는 GET 마스킹(`***`)·PUT keep-sentinel 로 평문 키가 브라우저에 안 나감,
    폼 밖 키 보존.
  - `models.json`: endpoint `GET /models` 프로브와 병합해 served/missing/NEW 분류.
    missing 은 개별/일괄 삭제(항상 confirm — 자동 삭제 없음), NEW 는 **agent-cli 의
    capability 탐지기 재사용**(`_detect_runtime_capabilities` lazy import, executor
    오프로드)으로 entry 초안 생성 → 검토 후 저장, 실패 시 수동 입력 폴백.
  - 도메인 로직은 `agent_board/admin.py` (FastAPI 무의존), 라우트
    `GET/PUT /api/admin/config`·`GET /api/admin/models`·`POST …/models/detect`·
    `PUT/DELETE /api/admin/models/{id}`. 쓰기는 원자적(mkstemp+replace — agent-cli
    인스턴스의 auto-detect 동시 저장과 안전). `Config.agent_cli_config_json` knob 추가.
  - 프로브 실패는 degrade(상태 `unknown`·registry 편집은 계속 가능), Anthropic 은
    `/v1/models`+`x-api-key` 로 프로브.

## [1.10.0] - 2026-07-05

### Added

- **Caddy 게이트웨이도 idle-reap 인스턴스를 자동 재기동(revive)** — board-proxy 와 파리티.
  `LiveEvents` 스캐너가 pid alive→dead 엣지를 감지하면 `on_death`→`router.remove_route` 로
  Caddy 동적 라우트를 삭제(≤1s), 그러면 Caddyfile 의 `everything→board` catch-all 이 보드로
  흘려 새 `/s/{id}` revive 핸들러가 `orchestrator.open`(spawn-or-attach + 라우트 재등록) 후
  302 redirect → 재시도가 살아난 라우트에 안착. GET/HEAD 한정(POST body 재생 불가 → 503),
  `?__revive=1` sentinel 로 무한루프 차단. 예전엔 Caddy 에서 죽은 방 직접 재접속이 **502** 였음.
- **`Router` ABC + 교차 파리티 테스트** — 두 라우터(board-proxy·caddy)는 메커니즘이 완전히
  달라 동작이 조용히 갈릴 수 있다(위 revive 가 그 사례였음). `Router(ABC)` 를 **순수 인터페이스**
  (공유 구현 0)로 두고 `ensure_route`/`remove_route`/`set_reopen`/`mount` 를 `@abstractmethod`
  로 선언 → 메서드 누락 구현은 **인스턴스화 시 `TypeError`**(구조적 강제). 인터페이스로 표현
  못 하는 창발 동작은 `tests/test_router_parity.py` 가 두 라우터를 parametrize 해 고정
  (down→GET→reopen 호출). "새 기능은 abstractmethod, 새 동작은 parity 테스트" 규율.

### Fixed

- **라우터 httpx 클라이언트가 종료 시 안 닫히던 gap** — `BoardProxyRouter.aclose()`(공유
  AsyncClient 정리)가 정의만 있고 **어디서도 호출되지 않았다**(보드 재시작마다 클라이언트 누수).
  `aclose` 를 `Router` 계약(abstractmethod)으로 올리고 `CaddyRouter` 에도 구현(sync 클라이언트
  close), lifespan finally 에서 `await router.aclose()` 로 배선. 파리티 작업 중 표면 비대칭으로
  드러난 건.

## [1.9.1] - 2026-07-05

### Added

- **기동 로그에 활성 게이트웨이 표시** (`gateway_banner`) — `board-proxy`(기본, in-process
  프록시) 인지 `caddy`(admin 주소 포함) 인지 한 줄로 출력. 예전엔 무표시라 실제로는 board-proxy
  로 돌면서 Caddy 로 착각하기 쉬웠음.

### Docs

- `docs/DESIGN.md` §9 에 **두 게이트웨이의 기능 동등성 표** 추가 — 전송(SSE 무버퍼·업로드·
  라우트 등록)은 같지만, board-proxy 만 **idle-reap 방을 접속 시 자동 재기동**하고 caddy 만
  **TLS·`/s/<id>` basic-auth** 를 제공(전환 시 UX/보안 차이 유의).
- `README.md` 설정 표 정정 — `AGENT_BOARD_DATA` 기본값 오기(`$HOME`→`base`/`./data`) 수정 +
  누락 변수(`GATEWAY`·`CADDY_ADMIN`·`CADDY_BASIC_AUTH`·`MODELS_JSON`) 추가.

## [1.9.0] - 2026-07-05

### Added

- **싱글 인스턴스 가드 (`board.lock` flock)** — 같은 `data_dir` 에서 agent-board 를 두 번째로
  띄우면 즉시 거부한다(보유 pid 안내 후 종료). `main()` 시작 시 `<data_dir>/board.lock` 에
  배타 `flock`(`acquire_singleton_lock`)을 걸어 프로세스 수명 동안 보유.
  - **왜**: per-post spawn 락이 프로세스-로컬 `asyncio.Lock` 이라 두 보드 사이엔 조율이 안 됨
    → 같은 `board.db` 를 경쟁하고, 같은 글을 동시에 open 하면 한 워크스페이스에 agent-cli
    인스턴스가 **이중 spawn**(세션 파일 경합)될 수 있었다. 가드가 그 근원(두 프로세스 공존)을 차단.
  - flock 은 보유자 사망 시 커널이 자동 해제 → **stale 락 수동정리 불필요**(pidfile 단독의 약점 회피).
  - 여러 보드가 필요하면 `AGENT_BOARD_DATA`/`AGENT_BOARD_WORKSPACES` 를 분리.

## [1.8.0] - 2026-07-05

### Changed

- **목록을 5초 폴링 대신 SSE 라이브 push 로 갱신** (`live_events.py`) — 프론트가 매 5초
  `/api/posts` 를 통째로 다시 불러오던 것을 걷어내고, 보드가 변화를 감지해 바뀐 행만
  밀어주는 방식으로 바꿨다. 백그라운드 스캐너(`LiveEvents`)가 1초마다 각 글의 **on-disk
  시그니처**(`status.json` mtime + `history.jsonl` mtime + pid 생존)를 표집해, 바뀐 글만
  `_post_view` 로 재계산 → `/api/events`(SSE) 구독자에게 `post_update`/`post_removed` 로
  broadcast 한다. 프론트는 `EventSource` 로 받아 해당 카드만 in-place 교체/삭제.
  - **pid-생존 항**: `status.json` 정리 없이 죽은(SIGKILL) 인스턴스도 `off` 로 확실히 뒤집음.
  - **heartbeat 워치독**: 주기 full-list 폴링을 없앤 대신 SSE 가 15초마다 `ping` 을 실어보내고,
    프론트 워치독이 30초 무수신 시 half-open(sleep/wake·불안정 망)으로 보고 강제 재연결한다.
    매 (재)연결마다 프론트가 한 번 full `load()` 하므로 공백 구간 이벤트도 유실 없음.
  - **비침습**: agent-cli 무수정. 스캔은 executor(별 스레드)에서 돌려 이벤트 루프를 안 막고,
    스캔 예외는 삼켜 루프를 유지한다. (보드 폴링 제거 로드맵 Phase 2 — 1단계는 v1.7.0.)

## [1.7.0] - 2026-07-05

### Changed

- **인스턴스 라이브 상태를 `/api/health` 폴링 대신 `status.json` 파일로 읽음** — 목록의
  status/busy/awaiting/viewers 를 매 새로고침마다 인스턴스별 HTTP `GET /api/health` 로
  폴링하던 것을, agent-cli(≥ 4.27.0)가 기록하는 `<session_dir>/status.json` **로컬 파일 read**
  로 대체했다. `instances.read_status_json` 추가, `sessions.live_state` 가 파일을 우선 읽고
  **없으면(옛 인스턴스) 기존 `/api/health` 로 폴백**(같은 `{busy, awaiting_input, viewers}`
  형태) → N개 인스턴스 × 새로고침마다의 HTTP 팬아웃 소멸. (보드 폴링 제거 로드맵 1단계.
  Phase 2 예정: FS watch + 보드 SSE 로 프론트까지 push.)

## [1.6.0] - 2026-07-05

### Removed

- **새 글 폼의 DIRECTIVE.md 입력 제거** — 2줄 textarea 로 directive 를 손으로 통짜
  입력해 글 생성 시 `<workspace>/.agent-cli/DIRECTIVE.md` 로 기록하던 경로를 걷어냈다.
  이 방식은 사실상 쓰이지 않았고, agent-cli 세션 내 **Directives 드로어**(성격·업무·지침
  3축 + 템플릿·프리셋·학습)가 같은 파일을 훨씬 나은 UX 로 라이브 편집하므로 **중복이자
  열등**했다. 프론트(textarea·JS·CSS), 백엔드(`NewPost.directive`, create_post 의
  DIRECTIVE.md 기록), 데이터 모델(`Post.directive`), 스토어(스키마·`_COLS`·INSERT)에서
  전부 제거.
  - **마이그레이션 불필요·resume 안전**: `posts` 테이블의 기존 `directive` 컬럼은 그대로
    두되 더 이상 SELECT/INSERT 하지 않는다 → 옛 DB 도 그대로 열린다. 기존 워크스페이스에
    이미 기록된 `DIRECTIVE.md` 파일도 삭제하지 않으므로 그 방들은 계속 적용된다.
  - **세션별 지시를 넣는 법**: agent-cli 세션의 Directives 드로어, 또는 전역
    `~/.agent-cli/DIRECTIVE.md`.

### Fixed

- **버전 정합성** — `__init__.__version__` 이 1.5.1 로 뒤처져 있던 것을 pyproject 와
  맞춰 정정(이번 릴리스에서 1.6.0 로 동기).

## [1.5.2] - 2026-07-04

### Fixed

- **🔄 재실행에 진행/완료 피드백 추가** — 재실행 버튼을 눌러도 아무 표시가 없어 눌렸는지조차
  알 수 없었다(빠른 respawn 이면 상태가 이미 "running" 이라 변화도 안 보임). 이제 누르는
  즉시 버튼이 **링 스피너**로 바뀌어 재시작 진행을 보여주고(백엔드가 stop+respawn 을 await
  하므로 스피너가 실제 작업 시간을 덮음), 완료되면 하단에 **토스트**(`🔄 재실행되었습니다 —
  <주제>`)로 확정 표시한다. 실패 시 `alert` 대신 에러 토스트. 스피너는 `visibility` 트릭으로
  🔄 이모지를 감추고 그 자리에 얇은 링만 돌려(버튼 박스 크기 유지 → 레이아웃 안 튐), 이모지
  통짜 회전 대신 깔끔한 로딩 스피너로 보인다. 프론트(app.js/style.css)만 변경.

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
