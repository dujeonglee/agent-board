# agent-board 레퍼런스

[README](../README.md) 의 설치·사용 예시 밖의 상세. 배포(LAN·인증·TLS)는 [DEPLOY.md](DEPLOY.md),
모듈·API·DB 설계는 [DESIGN.md](DESIGN.md).

- [카드와 상태](#카드와-상태)
- [새 글과 노브](#새-글과-노브)
- [방 열기·재실행·유지·삭제](#방-열기재실행유지삭제)
- [복제](#복제)
- [예약](#예약)
- [admin](#admin)
- [탭 가드](#탭-가드)
- [환경변수](#환경변수)
- [로그](#로그)
- [agent-cli 버전별 기능](#agent-cli-버전별-기능)

agent-cli 는 **외부 CLI 로만 호출**하며 수정하지 않는다.

## 카드와 상태

- 인스턴스는 **온디맨드로 떴다가 idle 시 스스로 종료**(agent-cli `--idle-timeout`),
  다음 접속에 `--resume` 으로 재기동. 보드를 재시작해도 살아있는 인스턴스에 재attach.
- 게시글마다 **상태 표시**: 🔵 Responding(main LLM 생성) / 🟠 Checking result(main 유휴,
  세션 감사 런 진행 — agent-cli ≥ 10.42.0) / 🟣 Agents working(main 유휴,
  상주 에이전트 가동 — agent-cli ≥ 7.10.0) / 🟢 Idle / ⚪ Off. 방이 ask/confirm 답을 기다리면
  이 라벨들보다 앞서 `❗ Needs your reply` 가 뜬다.
- 떠 있는 방에는 **실시간 접속자 수**(👁)와 **상주 에이전트 칩**(`🤖 작업중/전체`, hover 시
  에이전트별 상태 — 없으면 숨김)가 붙는다.
- 목록은 **SSE 라이브 push** 로 갱신 — 프론트가 주기적으로 폴링하지 않고, 보드가 각 글의
  on-disk 상태(mtime)를 감시해 바뀐 행만 즉시 밀어준다.
- 카드의 배지·칩(방이 `status.json` 에 쓴 값을 거울로 보인다):
  - 📐 — 문법 제약이 실리는 세션(status.json·health 의 `grammar`, agent-cli ≥ 9.24.0).
  - `📋 N` — 모델이 아직 갚을 열린 항목(status.json 의 `open_items`, agent-cli ≥ 10.119.0).
    툴팁에 id, 되묻기 예산이 0 이면 주황. 상세는 방의 📋 띠.
  - `🔁 N turns` — main 런이 새 결과 없이 여러 턴째 돈다(status.json 의 `stuck`,
    agent-cli ≥ 10.120.0). 방이 Continue/Stop 을 묻는 중이면 주황.
  - `🧵 N` — 방에 백그라운드 작업이 돌고 있다(status.json 의 `jobs`, agent-cli ≥ 10.121.0).
    툴팁에 작업마다 명령과 시작 시각. 뷰어 0 인데 방이 안 꺼지는 이유다. 멈추는 곳은 방의
    🧵 칩 ■.
  - `⏰ N` — 그 방의 예약 수([예약](#예약)).
- **버전 표시**: 헤더에 `v<board> · cli <agent-cli>` 를 표시한다 — board 자신과 board 가 방을
  spawn 하는 `agent-cli` 바이너리 버전(`GET /api/version`; cli 는 `agent-cli --version` 을
  프로세스당 1회 캐시, 부재/미인식이면 board 만).

## 새 글과 노브

**새 글**: 주제 + **모델(필수)** + `⚙ Advanced`(선택) → 빈 워크스페이스 자동 생성.

- 모델 드롭다운은 agent-cli 레지스트리(`~/.agent-cli/models.json`,
  `AGENT_BOARD_MODELS_JSON` 로 변경)에서 채워지며, 고른 id 가 spawn 시 `--model` 로
  전달됨(키/정의는 agent-cli 가 자기 레지스트리에서 해석 — 보드에 안 흩어짐).
- **보드는 방의 거울**(agent-cli ≥ 10.46.0 짝): `⚙ Advanced` 을 펼치면 사고·문법·
  샘플링·에이전트 상한·압축 비율·무진전 한도/시도·자동 승인·감사 노브를 줄 수 있다
  (컨트롤은 `agent-cli knobs --json` 의 표에서 그려지고, 비우면 agent-cli 기본값). 노브
  입력은 **여기 한 번**뿐이다. 만든 뒤에는 방 안의 칩(🤖 🧠 📐 👥 🗜️ ⏳ 🔎 ⚡)에서
  바꾸고, 보드는 방이 `status.json` 에 보고한 값을 카드에 🤖 모델 칩·노브 칩·`⚙`
  읽기 전용 패널로 보여 주며 **기록**한다 — 그래서 방에서 🤖 로 바꾼 모델이 재시작
  뒤에도 남는다. 카드에는 모델 드롭다운이 없다.
  뜰 수 없는 방(모델 삭제·방언 미설정)은 📋 Clone 으로 다른 모델을 골라 이어간다. 복제
  폼은 원본의 노브로 채워진다. 구버전 agent-cli(< 10.45.0)에서는 `⚙ Advanced` 이 안 보이고
  모델만 넘긴다.
- 드롭다운에는 **방언(dialect) 바인딩이 있는 모델만** 보인다(agent-cli ≥ 10.4.0 짝) —
  agent-cli 는 바인딩 없는 모델을 부트에서 거부하므로, 보드는 같은 판정으로 선택지에서 빼고
  (하나뿐이면 미리 고름, 없으면 `⚠ No model has a dialect — set one in ⚙ admin` 안내로
  만들기 비활성), 서버도 생성(400)과 열기·🔄 재실행(409)을 사유와 함께 거절한다.
  "(기본)" 선택지는 없다 — agent-cli `config.json` 의 `default_model` 에 기대지 않는다.
- 모델이 없거나 바인딩이 빠진 글은 카드에 ⚠ 경고 줄과 **⚙ Set up** 링크
  (`/admin#model=<id>` — 그 모델의 편집 창이 바로 열림)가 뜨고 Open 이 비활성이다.
  인스턴스가 준비되지 못하면(부트 fail-fast) 열기 실패 토스트에 `instance.log` 꼬리의
  사유가 실린다(502).
- 기존 코드는 글을 연 뒤 📁 업로드 또는 에이전트에게 `git clone` 으로 투입.

## 방 열기·재실행·유지·삭제

- **Open**: spawn-or-attach 후 `/s/<post_id>/` 로 접속 → agent-cli UI. 헤더의
  `Open in this page` 를 켜면 새 탭 대신 현재 탭에서 연다.
- **🔄 재실행**: 떠 있는 인스턴스를 **강제 재시작**(stop → respawn) — 새로 설치한
  agent-cli 를 반영할 때 쓴다. **게이트 없이 언제든** 동작하며(응답 중·접속자 있어도),
  **토큰을 재사용**해 이미 열려 있던 뷰어는 그대로 재연결되고 세션은 `--resume` 으로
  이어진다(대화 맥락 유지). 버튼은 인스턴스가 떠 있을 때만 보인다(꺼진 글은 Open 이
  이미 새로 spawn 하므로).
- **Keep**(force-active): 접속자 0 이어도 인스턴스를 살려둠(idle 종료 방지).
- **🗑 삭제**: 글 + 인스턴스 종료 + 워크스페이스 삭제.
- 방 제목: agent-cli ≥ 10.71.0 이면 글의 **제목(topic)을 `--title` 로 넘겨** 방의 헤더 첫
  자리·브라우저 탭(`<제목> · agent-cli`)·배너에 보인다. 제목의 주인은 글이고 spawn 마다
  넘기므로, 제목을 바꾸면 다음 🔄 재시작에 들어온다 — 떠 있는 방엔 쓰지 않는다(거울 규칙).
  구버전 agent-cli 엔 넘기지 않는다.

## 복제

각 글 카드의 `📋 Clone` 버튼(Open 옆)이 그 글을 원본으로 **모달**(`📋 Clone room`)을 연다.

- 한 창에서 새 주제·모델·복사할 파일/폴더(체크박스 트리)를 다 설정하고 `Create clone`.
  닫기(✕/Esc)나 `Cancel` = 중단 — 바깥 클릭으로는 닫히지 않는다.
- 대화를 이어받을 세션은 **원본 글에 저장된 session_id** 의 디렉터리다. 원본 글에 세션이
  없으면 복사된 세션 디렉터리가 하나일 때만 그것을 쓰고, 여럿이면 고르지 않고 fresh.
- `.agent-cli/sessions/…` 를 포함하면 **대화까지 이어받고**(세션 remap: 새 sid + `_meta`
  재작성, stale 사이드카 제외), 안 고르면 **파일만 가져온 새 대화**.
- **진행바**: 복제는 서버의 백그라운드 작업이라 `Create clone` 뒤 모달에 진행바와
  `Cloning… 12.3M / 48.0M · 37 / 120 files · <지금 파일>` 줄이 나오고, 끝나면 모달이 저절로
  닫힌다. 복제 중 `Close`(✕·Esc 도 같음)는 모달만 닫고 작업은 계속된다 — 그 글의 카드가
  `Cloning… N%` 와 작은 바를 보여 주며, 끝날 때까지 Open·🔄·📋 Clone 은 숨는다(서버도 409
  `cloning`). 실패하면 방은 롤백되고 모달에 사유가 나온다 — 모달을 닫은 뒤의 실패는 토스트
  `Clone failed: <사유>` 로 알린다.
- **심링크는 링크로 복사된다**: 링크가 가리키는 내용은 따라가지 않고 대상 문자열 그대로 다시
  만든다 — 안·밖·깨진 링크 모두 같다. 트리에서는 🔗 로 보이고 고를 수 있지만 펼칠 수는 없다;
  상대 링크는 대상도 함께 고르면 새 방 안에서 풀린다.
- 보드를 재시작하면 진행 중 작업은 사라지고 반쪽 방이 남는다(복구 없음).

## 예약

예약은 agent-cli 의 기능이다(agent-cli ≥ 10.12.0 필요 — 그 전 버전에는 예약 기능이 없다).
예약은 그 방의 agent-cli 세션이 소유하고 **그 프로세스가 발화**한다. 목록·추가·"지금
실행/건너뛰기" 는 방 화면 헤더의 ⏰ 서랍에서 하고, 대화 중 에이전트에게 "매주 월요일 9시에
주간 보고 써줘" 처럼 말해도 된다. 보드가 하는 일은 셋뿐이다:

- **배지**: 카드의 ⏰ 숫자(그 방의 예약 수). 누르면 방을 연다.
- **놓친 예약 안내**: 방이 꺼져 있던 동안 지난 발화는 자동 실행되지 않는다.
  카드에 `⏰ Missed schedule: …` 안내가 뜨고 `Answer in the room` 으로 들어가
  실행/건너뛰기를 고른다.
- **다시 띄우기**: 켜진 예약이 있는 방은 스스로 꺼지지 않는다(Keep 과 같은 효과). 그래도
  죽으면(재부팅·크래시) 보드가 기동 때와 죽음을 감지했을 때 다시 띄운다(방 하나당 60초에
  한 번).

v1.33 까지는 보드가 스케줄러였다(DB 테이블 + 워크스페이스 파일 계약). 그 예약은 v1.34.0 첫
기동 때 각 방의 세션 폴더로 옮겨지고 테이블은 지워진다(한 번도 연 적 없는 방의 예약은 옮길
세션이 없어 로그에 남기고 버린다). 설계: agent-cli 의 `docs/schedule/DESIGN.md` (종전 설계
[schedule-design.md](schedule-design.md) 는 기록으로 남긴다).

## admin

`/admin`(헤더 우측 ⚙): agent-cli 의 `config.json`/`models.json` 편집 페이지.

- **config.json**: provider/base_url/api_key/default_model 4필드 폼 — api_key 는
  화면에 `***` 로만 표시되고, `***`/빈값으로 저장하면 기존 키 유지. 그 외 키는 보존.
  변경은 **새로 여는 인스턴스부터** 적용(떠 있는 방은 🔄 재실행).
- **models.json** — 상태: config 의 endpoint 에 `GET /models` 프로브를 합쳐 모델별 상태 표시 —
  `served`(서빙 중) / `missing`(서버에서 사라짐 — 개별 🗑 또는 `🗑 Clean up all missing`) /
  `NEW`(서버에만 있음 — **`🔍 Detect`** 버튼이 agent-cli 의 capability 자동 탐지를 돌려
  entry 초안을 채우고, 검토·수정 후 저장; 탐지 실패 시 수동 입력).
- **models.json** — 편집: ✎ 다이얼로그(context_window/max_output_tokens/thinking/budget/format +
  **dialect(방언) 바인딩 드롭다운 — 필수** — 이 모델의 응답 tool-call 모양(agent-cli 모델별
  바인딩, 키 `dialect`). 없으면 agent-cli 가 그 모델을 실행하지 않으므로 저장이 거절되고(400),
  표에는 ⚠ 미설정 배지가, 편집 창에는 선택지별 한 줄 가이드(`json_fc` 일반 권장 / `native_fc`
  서버가 tool_calls 를 직접 파싱할 때 / `xml_fc` 태그 모양 선호 모델 …)가 보인다. `auto`
  선택지와 옛 키 `wire_format` 읽기는 없다(agent-cli v10.4.0 과 같은 판정). 옵션은 agent-cli
  등록 방언명만(자유입력 금지 — 오타는 agent-cli 부트 fail-fast 대상).
- **models.json** — 표의 열: `grammar` 열(`supports_grammar` ✓/✗/`?`=키 없음), 편집 폼에
  3값 선택(미기록 → 인스턴스에서 미확인으로 잠김, 감지(프로브)가 판정해 채움 / true / false).
  `vision` 열·`supports_vision` 3값 선택도 같은 모양 — 미기록이면 인스턴스가 첫 이미지에서
  한 번 프로브해 판정을 엔트리에 적는다(agent-cli ≥ 10.68.0). `sampling` 열(`T 0.2 · p 0.95` /
  `—`)과 편집 폼의 `temperature`(0~2)·`top_p`(0~1) 숫자 칸은 **이 모델의 기본 샘플링**
  (agent-cli ≥ 10.69.0) — 방이 자기 🎲 노브로 정하지 않은 키에 매 요청 실리고, 빈 칸 = 키
  미기록(서버 기본), 범위 밖은 저장 거절(400). 이 두 키는 하니스의 프로브가 채우지 않는
  사용자 결정값이다.
- 자동 삭제는 없음 — 모든 파괴적 동작은 confirm 클릭으로. 쓰기는 원자적(temp+replace)이라
  agent-cli 인스턴스의 auto-detect 저장과 겹쳐도 안전.

## 탭 가드

board-proxy 게이트웨이에서는 모든 방이 한 origin 으로 프록시되고 방/대시보드 탭이 각각 SSE
연결 1개를 점유하는데, 브라우저는 HTTP/1.1 origin 당 동시 연결이 **6개**(프로필 전체 합산)
뿐이라 6개가 차면 승인 클릭까지 모든 요청이 조용히 멈춘다.

- **운용 전제: 방은 항상 board 의 Open 버튼으로 접근.**
- 열기 전에 BroadcastChannel 로 연결 보유 탭(방 + 대시보드)을 세어(샘플링 창 100ms — pong 은
  같은 브라우저 전달이라 유휴 탭이 ~5ms 에 답하므로 충분) 이미 **4개면 경고, 5개 이상이면
  열지 않는다**(6이 되는 순간 포화라 1슬롯 예약).
- 게이트·POST 로 방 URL 을 완성한 뒤 **새 탭을 그 URL 로 바로** 연다. 같은 글을 다시 열면
  새 탭 대신 **기존 창을 재사용**(named window — 연결이 안 늘어 가드 면제).
- gateway=caddy(h2) 모드에선 가드가 자동 해제된다(`/api/gateway`).
- URL 직접 진입·브라우저 세션 복원은 게이트 밖(운용 정책상 범위 밖 — 세션 복원으로 탭이
  대량 재진입하면 포화가 재현될 수 있고, 그때는 탭을 닫으면 된다).

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| **─ 네트워크 ─** | | |
| `AGENT_BOARD_HOST` | `127.0.0.1` | 바인드 호스트. board-proxy(기본)는 인증이 없어 비-loopback 바인드를 거부한다 — LAN 노출은 `gateway=caddy`(인증, [DEPLOY.md](DEPLOY.md)) 또는 `AGENT_BOARD_ALLOW_UNAUTH_LAN=1`(명시 위험 감수) |
| `AGENT_BOARD_ALLOW_UNAUTH_LAN` | (없음) | `1` 이면 board-proxy 의 비-loopback 바인드를 허용한다 |
| `AGENT_BOARD_PORT` | `51966` (0xCAFE) | 보드 포트. 생략 시 51966 이 차 있으면 빈 포트로 넘어간다. 정하면 그 포트만 쓴다(충돌 시 실패) |
| **─ 경로 · 저장 ─** | | |
| `AGENT_BOARD_HOME` | `./data` | 데이터 루트 base (아래 DATA·WORKSPACES 의 기본값 base) |
| `AGENT_BOARD_DATA` | `= HOME`(`./data`) | `board.db`·`board.log`·`board.lock` 위치 (data_dir) |
| `AGENT_BOARD_WORKSPACES` | `<base>/ws` | 글별 워크스페이스 루트 (`<root>/<post_id>`) |
| `AGENT_BOARD_MODELS_JSON` | `~/.agent-cli/models.json` | 모델 드롭다운 목록 소스(agent-cli 레지스트리) — admin 이 편집하는 파일 |
| `AGENT_BOARD_AGENT_CLI_CONFIG` | `~/.agent-cli/config.json` | admin 이 편집하는 agent-cli `config.json` |
| **─ 인스턴스 spawn ─** | | |
| `AGENT_BOARD_CLI` | `agent-cli` | spawn 바이너리 (PATH 또는 절대경로) |
| `AGENT_BOARD_IDLE_TIMEOUT` | `300` | 인스턴스 `--idle-timeout` (초). viewer 0 이 이만큼 지속되면 자가종료 |
| **─ 게이트웨이 (라우팅) ─** | | |
| `AGENT_BOARD_GATEWAY` | `board-proxy` | 라우팅 데이터 플레인: `board-proxy`(보드 in-process 프록시) 또는 `caddy`(외부 Caddy). [DEPLOY.md](DEPLOY.md) |
| `AGENT_BOARD_CADDY_ADMIN` | `http://127.0.0.1:2019` | Caddy admin API (`gateway=caddy` 일 때만) |
| `AGENT_BOARD_CADDY_BASIC_AUTH` | `""` | Caddy 라우트 basic-auth `user:bcrypt` (`gateway=caddy` 일 때만) |

> **한 `data_dir` = 보드 1개.** 같은 data_dir 에서 보드를 두 번 띄우면 둘째는
> `board.lock`(flock) 에 걸려 즉시 거부된다(공유 `board.db` 경쟁 + 인스턴스 이중 spawn
> 방지). 여러 보드가 필요하면 `AGENT_BOARD_DATA`/`AGENT_BOARD_WORKSPACES` 를 분리할 것.

## 로그

로그는 콘솔이 아니라 파일로 빠진다 — 콘솔엔 startup·에러만:

- access 로그 → **`<DATA_DIR>/board.log`**(회전 5MB×3)
- spawn 된 인스턴스 출력(배너 등) → **`<workspace>/.agent-cli/instance.log`**

## agent-cli 버전별 기능

보드는 agent-cli 가 쓰는 파일·API 를 읽는다. 아래 버전보다 낮으면 그 줄의 기능이 빠진다.

| agent-cli | 보드에서 쓰는 것 |
|---|---|
| ≥ 4.17.3 | 상태 표시(busy)·뒤로가기 정리 |
| ≥ 4.17.11 | 접속자 수 표시(`/api/health` 의 `viewers`) |
| ≥ 4.27.0 | 라이브 상태를 `status.json` 파일에서 읽어 `/api/health` 폴링을 안 함(그 미만은 자동으로 health 폴백) |
| ≥ 7.10.0 | 🟣 Agents working·🤖 에이전트 칩 |
| ≥ 9.24.0 | 📐 문법 배지 |
| ≥ 10.4.0 | 방언 바인딩이 있는 모델만 고르기 |
| ≥ 10.12.0 | ⏰ 예약 |
| ≥ 10.42.0 | 🟠 Checking result |
| ≥ 10.45.0 | `⚙ Advanced` 노브(`agent-cli knobs --json`) |
| ≥ 10.68.0 | admin 의 `vision` 프로브 |
| ≥ 10.69.0 | admin 의 모델 기본 샘플링 |
| ≥ 10.71.0 | 방 제목(`--title`) |
| ≥ 10.119.0 | 📋 열린 항목 칩 |
| ≥ 10.120.0 | 🔁 제자리 배지 |
| ≥ 10.121.0 | 🧵 작업 칩 |

모델 백엔드(omlx-server 등)는 agent-cli 설정을 그대로 사용한다.
