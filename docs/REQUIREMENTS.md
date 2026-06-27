# agent-board — REQUIREMENTS

> 게시판형 웹 서비스. **글 하나 = agent-cli 세션 하나 + workspace + 주제.**
> 글을 클릭하면 그 세션의 agent-cli web UI 에 접속한다. agent-cli 는 **외부 CLI 로만
> 호출**하며 수정하지 않는다(필요한 기능은 이미 agent-cli 측에 구현 완료).

---

## 1. 개요 · 컨셉
- 포럼/게시판처럼 **글 목록**을 보여주고, 글을 클릭하면 해당 agent-cli web 에 접속.
- 글 하나 = **주제(topic) + workspace + agent-cli 세션(session_id)** 의 묶음.
- 인스턴스는 **온디맨드로 떴다가 idle 시 스스로 종료**, 다음 접속에 `--resume` 으로 재기동.
- **별도 레포 · Python**(FastAPI 권장 — agent-cli 패턴 재사용).

## 2. 아키텍처 (확정)
**v1 = 보드가 직접 프록시(B).** 라우팅 데이터 평면을 `Router` 인터페이스 뒤에 둬서,
v1 은 `BoardProxyRouter`(무의존, in-process e2e 테스트 쉬움)로 가고, 프로덕션은
`CaddyRouter`(게이트웨이, TLS·단일포트·재시작 견고)로 **한 줄 스위치** 승급. (DESIGN §0/§9)

```
브라우저 (사내/VPN)
   ↓  (v1) 보드가 인증 + /s/<post_id>/* 를 인스턴스로 프록시(SSE 스트리밍)
        (prod) 그 자리에 Caddy 게이트웨이
[컨트롤 플레인]  agent-board (이 서비스) — 글 관리, 인스턴스 spawn, 라우팅, force-active
   ↓  127.0.0.1:<port>
[인스턴스]  agent-cli web (127.0.0.1 바인드, --idle-timeout 자가종료)
```

- **인스턴스 발견**: agent-cli 가 시작 시 `.agent-cli/sessions/<id>/web.json`
  (`{session_id, host, port, token, pid}`) 기록, 종료 시 제거.
- **스폰 명령(예)**:
  `agent-cli web --resume <id> --host 127.0.0.1 --idle-timeout <N> --trust-local --base-path /s/<post_id> --no-browser`
- **인증**: 게이트웨이가 전담. 인스턴스는 loopback 바인드 + `--trust-local`(토큰 plumbing 0).
- **라우팅**: 경로 prefix `/s/<post_id>/*`(게이트웨이가 strip → 인스턴스는 `/api/...` 수신).

## 3. v1 범위
- **글 목록**: 각 글에 **주제 + 마지막 쿼리 + 상태** 표시. (마지막 쿼리는 세션 history,
  상태는 🔵응답 중(LLM 생성)/🟢대기/⚪꺼짐 — `/api/health` 의 busy 로 유도)
- **새 글**: **주제, DIRECTIVE.md(선택)** 입력. **workspace 는 글마다 자동 생성**(사용자
  경로 입력 없음 — 5번 참조).
- **클릭 → 접속**: spawn-or-attach 후 보드 프록시 경유 접속.
- **idle 자가종료**: agent-cli `--idle-timeout`.
- **Force-active 체크박스**: 켜면 그 인스턴스를 **계속 살림**(idle 종료 방지), 끄면 해제.
- **글 삭제**: 글 제거 + **그 글의 워크스페이스 디렉토리도 삭제**(보드 소유라 항상 안전).

## 4. 데이터 모델
글(Post):
```
{
  post_id:       보드 발급 식별자 (라우트 /s/<post_id> 에 사용)
  topic:         주제 (사용자 입력)
  workspace:     <WORKSPACES_ROOT>/<post_id>  (post_id 에서 파생, 항상 새 디렉토리)
  session_id:    agent-cli 세션 id (첫 open 에 발견해 저장, 그 전엔 NULL)
  directive:     DIRECTIVE.md 내용 (선택)
  created_at:    생성 시각
  force_active:  bool
  // 파생(저장 안 함): last_query(세션 history), status(web.json+pid+health)
}
```
- **보드가 생성한 글만 관리**(기존 CLI 세션은 노출 안 함). 메타는 보드 자체 레지스트리에 저장.

## 5. Workspace — 항상 글마다 새로 생성 (사용자 경로 입력 없음)
- **글 생성 시 `<WORKSPACES_ROOT>/<post_id>/` 빈 디렉토리를 보드가 만든다.** 기존
  디렉토리를 workspace 로 지정하는 입력은 **제공하지 않는다.**
- **보안 — 원천 제거**: 사용자가 경로를 입력할 일이 없으므로 `/etc` 같은 **임의 경로
  지정·traversal·심볼릭 escape 위험이 아예 없음**. 보드가 건드리는 파일시스템은 오직
  `WORKSPACES_ROOT` 하위.
- **공유 방어 — 자동**: workspace 가 고유한 `post_id` 에서 파생되므로 두 글이 같은
  디렉토리를 점유할 수 없음(구조적 보장).
- **기존 코드 투입 방법**: 새 글은 빈 워크스페이스로 시작하므로, 코드는 **📁 드로어
  파일/폴더 업로드** 또는 **에이전트에게 `git clone` 지시**로 넣는다.
- **DIRECTIVE.md(선택)**: 글 생성 시 `<workspace>/.agent-cli/DIRECTIVE.md` 로 기록 →
  agent-cli 가 해당 세션 프롬프트에 자동 포함.

## 6. 인증 · 접근
- **개방형**: 누구나 **모든 글을 열람·참여** 가능 (v1 에 글별 소유권/권한 없음).
  접근 통제는 게이트웨이 레벨(팀 공용).
- **동시 다중 사용자**: 한 글에 여러 명 동시 접속 — agent-cli 다중 뷰어가 처리.

## 7. 오케스트레이션 (라이프사이클)
- **글 열기**: `web.json` 읽기 → 떠 있으면(pid 살아있음 + `/api/health` OK) 라우트
  연결/리다이렉트, 없으면 spawn → web.json/health 대기 → 연결.
- **회수**: agent-cli `--idle-timeout` 에 위임(보드는 kill 안 함). pid 죽은 web.json 은 stale → 재spawn.
- **force-active**: 컨트롤 플레인이 그 인스턴스에 **SSE 연결(`/api/stream`) 하나를 유지**
  → agent-cli 가 뷰어로 세어 idle 안 됨. 체크 해제 시 그 연결을 닫음.
  (워커가 작업 중이어도 안 죽는 기존 동작과 별개로, "사람 없어도 살려둠"을 보장.)
- **라우트 등록**: 인스턴스가 뜰 때 게이트웨이에 `/s/<post_id>` → `127.0.0.1:<port>` 동적 등록,
  종료 시 해제.

## 8. 모델 · 프로바이더
- **v1: 서버의 agent-cli 전역 설정** 그대로 사용(글별 모델 선택 없음).
- 글별 모델 선택 = v2.

## 9. 저장 · 배포 · 기술
- **언어**: Python (FastAPI 권장).
- **레포**: agent-cli 와 **별도 레포**.
- **글 메타 저장**: 보드 자체 레지스트리(파일/경량 DB — 구현 시 결정).
- **배포**: 단일 호스트(팀 규모). 게이트웨이 = Traefik 또는 Caddy(구현 시 결정).

## 10. 범위 밖 (v2+)
- 글별 접근 권한 / 소유권.
- 기존 CLI 세션 노출(보드 생성 글만 관리).
- 검색/필터, 실시간 접속자 표시(presence), 글별 모델 선택.
- 멀티-호스트 확장, 컨테이너 격리/쿼터(공개 SaaS 영역 — 현재 범위 아님).

## 11. agent-cli 측 선행 기능 (완료, 이 보드가 의존)
- `--idle-timeout N` (self-reap) — v4.13.0
- 인스턴스 파일 `web.json` (spawn-or-attach) — v4.14.0
- `--trust-local` (loopback 토큰 면제) — v4.15.0
- `--base-path <prefix>` (경로 prefix 라우팅) — v4.16.0
- `pagehide`→SSE 해제 (유령 viewer/누적 방지) — v4.17.1
- `/api/health` 의 `busy` (3단계 상태) — v4.17.2
- auto-review 인터럽트 정지 — v4.17.3
- **권장 인스턴스 버전: agent-cli ≥ 4.17.3**
