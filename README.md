# agent-board

게시판형 웹 서비스 — **글 하나 = [agent-cli](https://github.com/dujeonglee/agent-cli)
세션 하나 + 워크스페이스 + 주제**. 글을 클릭하면 그 세션의 agent-cli web UI 에 접속한다.

- 인스턴스는 **온디맨드로 떴다가 idle 시 스스로 종료**(agent-cli `--idle-timeout`),
  다음 접속에 `--resume` 으로 재기동. 보드를 재시작해도 살아있는 인스턴스에 재attach.
- 게시글마다 **상태 표시**: 🔵 응답 중(LLM 생성) / 🟢 대기 / ⚪ 꺼짐.
- agent-cli 는 **외부 CLI 로만 호출**하며 수정하지 않는다.

## 요구사항
- Python 3.10+
- **`agent-cli` 가 PATH 에 있어야 함** (보드가 `agent-cli web ...` 를 spawn).
  상태 표시(busy)·뒤로가기 정리까지 쓰려면 **agent-cli ≥ 4.17.3** 권장.
- 모델 백엔드(omlx-server 등)는 agent-cli 설정을 그대로 사용.

## 설치 · 실행
```bash
pip install -e ".[dev]"

python -m agent_board.app          # → http://localhost:8001
# 또는 콘솔 스크립트
agent-board
```

### 설정 (환경변수)
| 변수 | 기본값 | 설명 |
|---|---|---|
| `AGENT_BOARD_HOST` | `0.0.0.0` | 바인드 호스트 |
| `AGENT_BOARD_PORT` | `8001` | 보드 포트 (8000 은 omlx-server 가 흔히 점유) |
| `AGENT_BOARD_HOME` | `./data` | 데이터 루트 (board.db + workspaces 의 기본 base) |
| `AGENT_BOARD_DATA` | `$HOME` | `board.db` 위치 |
| `AGENT_BOARD_WORKSPACES` | `$HOME/workspaces` | 글별 워크스페이스 루트 |
| `AGENT_BOARD_CLI` | `agent-cli` | spawn 바이너리 |
| `AGENT_BOARD_IDLE_TIMEOUT` | `300` | 인스턴스 `--idle-timeout` (초). viewer 0 이 이만큼 지속되면 자가종료 |

## 사용
1. **새 글**: 주제 + (선택) DIRECTIVE.md → 빈 워크스페이스가 자동 생성됨.
   기존 코드는 글을 연 뒤 📁 업로드 또는 에이전트에게 `git clone` 으로 투입.
2. **열기**: spawn-or-attach 후 `/s/<post_id>/` 로 접속 → agent-cli UI.
3. **유지(force-active)**: 접속자 0 이어도 인스턴스를 살려둠(idle 종료 방지).
4. **🗑 삭제**: 글 + 인스턴스 종료 + 워크스페이스 삭제.

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
# 3) 보드를 caddy 모드로
AGENT_BOARD_GATEWAY=caddy \
AGENT_BOARD_CADDY_BASIC_AUTH='alice:$2a$14$...' \
agent-board
```
- 동봉: `deploy/Caddyfile`, `deploy/agent-board.service`(systemd).
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
