# agent-board 배포 — 게이트웨이 · caddy

기본 설치는 [README](../README.md), 환경변수 전부는 [REFERENCE.md](REFERENCE.md#환경변수).

## 아키텍처 · 게이트웨이

`AGENT_BOARD_GATEWAY` 로 라우팅 데이터 평면을 고른다:

- **`board-proxy`(기본)** — 보드가 직접 `/s/<id>/*` 를 SSE 무버퍼로 프록시. 무의존,
  로컬/소규모. 보드가 데이터 경로에 있음(재시작 시 연결 끊김→자동 재연결).
  인증이 없어 비-loopback 바인드를 거부한다(`AGENT_BOARD_ALLOW_UNAUTH_LAN=1` 로 명시 허용).
- **`caddy`(프로덕션)** — 보드가 Caddy admin API 로 `/s/<id>` 라우트를 등록, **Caddy 가
  인스턴스로 직접 프록시**. 보드는 데이터 경로 밖(TLS·단일포트·재시작 견고).

> **두 게이트웨이는 동작 파리티가 맞춰져 있다** — idle-reap 된 방을 직접 URL 재접속만으로
> 자동 재기동(revive)하는 것은 **양쪽 다** 된다(board-proxy 는 in-process,
> caddy 는 death 엣지에 라우트 삭제→보드 revive 핸들러로 fall-through). 남는 차이는 전송 특성:
> `caddy` 만 **TLS·단일포트·`/s/<id>` basic-auth** 를 준다. 기동 로그에 활성 게이트웨이가
> 표시되고, `Router` ABC + `tests/test_router_parity.py` 가 두 구현의 파리티를 강제한다.
> 자세히는 [DESIGN.md](DESIGN.md) §9.

## 프로덕션 배포 (caddy)

```bash
# 1) 비밀번호 해시 생성
caddy hash-password --plaintext 'secret'      # → $2a$14$...

# 2) deploy/Caddyfile 의 도메인·해시 수정 후 Caddy 기동 (admin 127.0.0.1:2019)
#    도메인 없는 LAN 이면 deploy/Caddyfile.lan 사용:
#      <LAN_IP> 치환 → caddy run --config deploy/Caddyfile.lan
#      접속: https://<LAN_IP>:8443 (h2 — 연결 6개 제한 소멸, 탭 가드 자동 해제)
#      기기당 1회 CA 신뢰: 이 맥 `caddy trust`, 타 기기는 root.crt 설치(또는 경고 클릭스루)
# 3) 보드를 caddy 모드로 (★ loopback 바인드 필수 — 아래 주의)
AGENT_BOARD_GATEWAY=caddy \
AGENT_BOARD_HOST=127.0.0.1 \
AGENT_BOARD_CADDY_BASIC_AUTH='alice:$2a$14$...' \
agent-board
```

명령은 저장소 루트에서 실행한다(`deploy/…` 경로).

- 동봉: [`deploy/Caddyfile`](../deploy/Caddyfile), [`deploy/Caddyfile.lan`](../deploy/Caddyfile.lan),
  [`deploy/agent-board.service`](../deploy/agent-board.service)(systemd, 이미 `127.0.0.1` 바인드),
  [`deploy/com.agentboard.plist`](../deploy/com.agentboard.plist)(macOS launchd).
- **★ caddy 모드는 보드를 `AGENT_BOARD_HOST=127.0.0.1` 로 바인드**하라. Caddy 가 앞단에서
  `/s/<id>` 를 인스턴스로 직결하는데, 보드 포트를 `0.0.0.0`/외부로도 열어두고 **그 포트에 직접
  접속하면 Caddy 를 우회**해 revive fall-through 가 같은 origin 으로 리다이렉트→503 루프가 된다.
  비-loopback 바인드 시 기동 로그가 경고한다(브라우저는 항상 Caddy 주소로 접속).
- **보안**: 각 `/s/<id>` 동적 라우트에 **basic_auth 핸들러가 직접 포함**되어(보드가 삽입)
  삽입 순서와 무관하게 **인증 우회 불가**. 단 단위테스트는 admin API 호출만 검증하므로,
  배포 후 **반드시** `curl` 로 인증을 실측하라(Caddyfile 하단 체크리스트 — 토큰 없이
  `/s/<id>/api/health` → 401 이어야 함).
