# agent-board

게시판형 웹 서비스 — **글 하나 = [agent-cli](https://github.com/dujeonglee/agent-cli)
세션 하나 + 워크스페이스 + 주제**. 글을 클릭하면 그 세션의 agent-cli web UI 에 접속한다.

- 인스턴스는 **온디맨드로 떴다가 idle 시 스스로 종료**(agent-cli `--idle-timeout`),
  다음 접속에 `--resume` 으로 재기동. 보드를 재시작해도 살아있는 인스턴스에 재attach.
- 게시글마다 **상태 표시**: 🔵 응답 중(LLM 생성) / 🟢 대기 / ⚪ 꺼짐, **실시간 접속자 수**(👁),
  그리고 **게시물별 모델 드롭다운**(아무도 안 볼 때만 변경 가능 — 아래 참고).
- 목록은 **SSE 라이브 push** 로 갱신 — 프론트가 주기적으로 폴링하지 않고, 보드가 각 글의
  on-disk 상태(mtime)를 감시해 바뀐 행만 즉시 밀어준다.
- **탭 가드 (v1.14.0)** — board-proxy 게이트웨이에서는 모든 방이 한 origin 으로
  프록시되고 방/대시보드 탭이 각각 SSE 연결 1개를 점유하는데, 브라우저는 HTTP/1.1
  origin 당 동시 연결이 **6개**(프로필 전체 합산)뿐이라 6개가 차면 승인 클릭까지 모든
  요청이 조용히 멈춘다. 그래서 열기 전에 BroadcastChannel 로 "연결을 잡은 탭"(방 탭은
  agent-cli ≥ 7.3.0 의 비콘, 대시보드 탭은 자체 응답)을 세어 **4개째 경고, 5개째 차단**
  한다(6이 되는 순간 포화). 같은 글을 다시 열면 새 탭 대신 **기존 창을 재사용**(named
  window — 연결이 안 늘어 가드 면제; 의도적 두 번째 창은 URL 복사로 가능). gateway=caddy
  (h2) 모드에선 연결 1개에 스트림이 멀티플렉스되므로 가드가 자동 해제된다(`/api/gateway`).
  한계: board 를 거치지 않은 진입(브라우저 세션 복원·URL 직접·탭 복제)은 게이트 밖 —
  그 경우는 agent-cli(≥7.2.0)의 confirm 무응답 경고가 상황을 알려준다.
- agent-cli 는 **외부 CLI 로만 호출**하며 수정하지 않는다.

## 요구사항
- Python 3.10+
- **`agent-cli` 가 PATH 에 있어야 함** (보드가 `agent-cli web ...` 를 spawn).
  상태 표시(busy)·뒤로가기 정리까지 쓰려면 **agent-cli ≥ 4.17.3**, **접속자 수 표시 +
  게시물별 모델 변경**까지 쓰려면 **agent-cli ≥ 4.17.11**(`/api/health` 의 `viewers`) 권장.
  **≥ 4.27.0** 이면 라이브 상태를 `status.json` 파일에서 읽어 **`/api/health` 폴링을 안 함**
  (그 미만은 자동으로 health 폴백).
- 모델 백엔드(omlx-server 등)는 agent-cli 설정을 그대로 사용.

## 설치 · 실행
```bash
pip install -e ".[dev]"

python -m agent_board.app          # → http://localhost:51966
# 또는 콘솔 스크립트
agent-board
```

> **한 `data_dir` = 보드 1개.** 같은 data_dir 에서 보드를 두 번 띄우면 둘째는
> `board.lock`(flock) 에 걸려 즉시 거부된다(공유 `board.db` 경쟁 + 인스턴스 이중 spawn
> 방지). 여러 보드가 필요하면 `AGENT_BOARD_DATA`/`AGENT_BOARD_WORKSPACES` 를 분리할 것.

### 설정 (환경변수)
| 변수 | 기본값 | 설명 |
|---|---|---|
| **─ 네트워크 ─** | | |
| `AGENT_BOARD_HOST` | `0.0.0.0` | 바인드 호스트 |
| `AGENT_BOARD_PORT` | `51966` (0xCAFE) | 보드 포트 (생략 시 fallback 다이나믹·인스턴스 50000~60000·omlx 8000·cli 0xC0DE 회피) |
| **─ 경로 · 저장 ─** | | |
| `AGENT_BOARD_HOME` | `./data` | 데이터 루트 base (아래 DATA·WORKSPACES 의 기본값 base) |
| `AGENT_BOARD_DATA` | `= HOME`(`./data`) | `board.db`·`board.log`·`board.lock` 위치 (data_dir) |
| `AGENT_BOARD_WORKSPACES` | `<base>/workspaces` | 글별 워크스페이스 루트 (`<root>/<post_id>`) |
| `AGENT_BOARD_MODELS_JSON` | `~/.agent-cli/models.json` | 모델 드롭다운 목록 소스(agent-cli 레지스트리) |
| **─ 인스턴스 spawn ─** | | |
| `AGENT_BOARD_CLI` | `agent-cli` | spawn 바이너리 (PATH 또는 절대경로) |
| `AGENT_BOARD_IDLE_TIMEOUT` | `300` | 인스턴스 `--idle-timeout` (초). viewer 0 이 이만큼 지속되면 자가종료 |
| **─ 게이트웨이 (라우팅) ─** | | |
| `AGENT_BOARD_GATEWAY` | `board-proxy` | 라우팅 데이터 플레인: `board-proxy`(보드 in-process 프록시) 또는 `caddy`(외부 Caddy). ⚠️ 아래 주의 |
| `AGENT_BOARD_CADDY_ADMIN` | `http://127.0.0.1:2019` | Caddy admin API (`gateway=caddy` 일 때만) |
| `AGENT_BOARD_CADDY_BASIC_AUTH` | `""` | Caddy 라우트 basic-auth `user:bcrypt` (`gateway=caddy` 일 때만) |

> **두 게이트웨이는 동작 파리티가 맞춰져 있다** — idle-reap 된 방을 직접 URL 재접속만으로
> 자동 재기동(revive)하는 것은 이제 **양쪽 다** 된다(board-proxy 는 in-process,
> caddy 는 death 엣지에 라우트 삭제→보드 revive 핸들러로 fall-through). 남는 차이는 전송 특성:
> `caddy` 만 **TLS·단일포트·`/s/<id>` basic-auth** 를 준다. 기동 로그에 활성 게이트웨이가
> 표시되고, `Router` ABC + `tests/test_router_parity.py` 가 두 구현의 파리티를 강제한다.
> 자세히는 `docs/DESIGN.md` §9.

> 로그는 콘솔이 아니라 파일로 빠집니다 — 콘솔엔 startup·에러만:
> access 로그 → **`<DATA_DIR>/board.log`**(회전 5MB×3),
> spawn 된 인스턴스 출력(배너 등) → **`<workspace>/.agent-cli/instance.log`**.

## 사용
1. **새 글**: 주제 + (선택) **모델** → 빈 워크스페이스 자동 생성.
   모델 드롭다운은 agent-cli 레지스트리(`~/.agent-cli/models.json`,
   `AGENT_BOARD_MODELS_JSON` 로 변경)에서 채워지며, 고른 id 가 spawn 시 `--model` 로
   전달됨(키/정의는 agent-cli 가 자기 레지스트리에서 해석 — 보드에 안 흩어짐).
   기존 코드는 글을 연 뒤 📁 업로드 또는 에이전트에게 `git clone` 으로 투입.
2. **열기**: spawn-or-attach 후 `/s/<post_id>/` 로 접속 → agent-cli UI.
3. **모델 변경**: 게시글 행의 모델 드롭다운으로 언제든 바꿀 수 있다 — 단 **아무도 보고 있지
   않을 때만**(인스턴스가 꺼져 있거나, 떠 있어도 접속자 0 + 응답 중 아님). 응답 중이거나
   접속자가 있으면 드롭다운이 **비활성**된다(👁 접속자 수로 확인). 변경 시: 꺼진 글은
   다음 열기에 새 모델로, 떠 있는 글은 인스턴스를 멈춰(⚪) 다음 열기에 적용 — 단
   **force-active 글은 즉시 새 모델로 재시작**(유지 약속 보존). 세션은 `--resume` 으로
   이어지므로 대화 맥락은 유지된 채 모델만 교체된다.
4. **🔄 재실행**: 떠 있는 인스턴스를 **강제 재시작**(stop → respawn) — 새로 설치한
   agent-cli 를 반영할 때 쓴다. **게이트 없이 언제든** 동작하며(응답 중·접속자 있어도),
   **토큰을 재사용**해 이미 열려 있던 뷰어는 그대로 재연결되고 세션은 `--resume` 으로
   이어진다(대화 맥락 유지). 버튼은 인스턴스가 떠 있을 때만 보인다(꺼진 글은 "열기"가
   이미 새로 spawn 하므로).
5. **유지(force-active)**: 접속자 0 이어도 인스턴스를 살려둠(idle 종료 방지).
6. **🗑 삭제**: 글 + 인스턴스 종료 + 워크스페이스 삭제.
7. **⚙ admin** (`/admin`, 헤더 우측): agent-cli 의 `config.json`/`models.json` 편집 페이지.
   - **config.json**: provider/base_url/api_key/default_model 4필드 폼 — api_key 는
     화면에 `***` 로만 표시되고, `***`/빈값으로 저장하면 기존 키 유지. 그 외 키는 보존.
     변경은 **새로 여는 인스턴스부터** 적용(떠 있는 방은 🔄 재실행).
   - **models.json**: config 의 endpoint 에 `GET /models` 프로브를 합쳐 모델별 상태 표시 —
     `served`(서빙 중) / `missing`(서버에서 사라짐 — 개별 🗑 또는 "missing 전체 정리") /
     `NEW`(서버에만 있음 — **🔍 탐지** 버튼이 agent-cli 의 capability 자동 탐지를 돌려
     entry 초안을 채우고, 검토·수정 후 저장; 탐지 실패 시 수동 입력). 편집은 ✎ 다이얼로그
     (context_window/max_output_tokens/structured/strict/thinking/budget/format +
     **wire_format 바인딩 드롭다운** — 이 모델의 응답 wire format(agent-cli v5.19.0
     모델별 바인딩); `auto` 는 필드 미기록 = 해석 체인 위임(기본 md_array). 옵션은
     agent-cli 등록 포맷명만(자유입력 금지 — 오타는 agent-cli 부트 fail-fast 대상).
   - 자동 삭제는 없음 — 모든 파괴적 동작은 confirm 클릭으로. 쓰기는 원자적(temp+replace)이라
     agent-cli 인스턴스의 auto-detect 저장과 겹쳐도 안전.

## 아키텍처 · 게이트웨이
`AGENT_BOARD_GATEWAY` 로 라우팅 데이터 평면을 고른다:

- **`board-proxy`(기본)** — 보드가 직접 `/s/<id>/*` 를 SSE 무버퍼로 프록시. 무의존,
  로컬/소규모. 보드가 데이터 경로에 있음(재시작 시 연결 끊김→자동 재연결).
- **`caddy`(프로덕션)** — 보드가 Caddy admin API 로 `/s/<id>` 라우트를 등록, **Caddy 가
  인스턴스로 직접 프록시**. 보드는 데이터 경로 밖(TLS·단일포트·재시작 견고).

### 프로덕션 배포 (caddy)
```bash
# 1) 비밀번호 해시 생성
caddy hash-password --plaintext 'secret'      # → $2a$14$...

# 2) deploy/Caddyfile 의 도메인·해시 수정 후 Caddy 기동 (admin 127.0.0.1:2019)
# 3) 보드를 caddy 모드로 (★ loopback 바인드 필수 — 아래 주의)
AGENT_BOARD_GATEWAY=caddy \
AGENT_BOARD_HOST=127.0.0.1 \
AGENT_BOARD_CADDY_BASIC_AUTH='alice:$2a$14$...' \
agent-board
```
- 동봉: `deploy/Caddyfile`, `deploy/agent-board.service`(systemd, 이미 `127.0.0.1` 바인드).
- **★ caddy 모드는 보드를 `AGENT_BOARD_HOST=127.0.0.1` 로 바인드**하라. Caddy 가 앞단에서
  `/s/<id>` 를 인스턴스로 직결하는데, 보드 포트를 `0.0.0.0`/외부로도 열어두고 **그 포트에 직접
  접속하면 Caddy 를 우회**해 revive fall-through 가 같은 origin 으로 리다이렉트→503 루프가 된다.
  비-loopback 바인드 시 기동 로그가 경고한다(브라우저는 항상 Caddy 주소로 접속).
- **보안**: 각 `/s/<id>` 동적 라우트에 **basic_auth 핸들러가 직접 포함**되어(보드가 삽입)
  삽입 순서와 무관하게 **인증 우회 불가**. 단 단위테스트는 admin API 호출만 검증하므로,
  배포 후 **반드시** `curl` 로 인증을 실측하라(Caddyfile 하단 체크리스트 — 토큰 없이
  `/s/<id>/api/health` → 401 이어야 함).

## 설계 문서
- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — 요구사항
- [docs/DESIGN.md](docs/DESIGN.md) — 모듈/API/DB/라이프사이클 설계

## 개발
```bash
pytest          # 전체 테스트
ruff check agent_board/ tests/ && ruff format --check agent_board/ tests/
```
