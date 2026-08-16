# Hypercorn 전환 — Caddy 없이 TLS+HTTP/2 embedding (계획)

상태: **구현 완료 (v1.28.0)** · 라이브 스모크(curl --http2 200/401·쿠키 핸드셰이크·SSE over h2·access 로그 회전) 검증. 아래는 원 계획 원문.

## 0. 목표 / 배경

Caddy를 별도 서버로 돌리지 않고 board 자체가 **TLS + HTTP/2 + 단일포트**를
제공한다. 라우팅(`/s/<id>` 프록시·SSE·revive)은 이미 `board-proxy`가 in-process로
하므로 Caddy가 유일하게 주던 것은 **TLS · HTTP/2 · 컨트롤플레인 basic_auth** 3가지.
그중 TLS·HTTP/2를 ASGI 서버 교체(uvicorn→Hypercorn)로 네이티브 흡수한다.

- uvicorn: HTTP/1.1 전용 → 브라우저 origin당 6-connection 한계 → 탭 가드 필요.
- Hypercorn: 순수 파이썬, **HTTP/2 + TLS 네이티브** → TLS 켜면 브라우저가 ALPN으로
  h2 협상 → 연결 1개 멀티플렉스 → 6-connection 한계 소멸 → 탭 가드 자동 해제.

**결정: `caddy` 게이트웨이는 제거하지 않는다**(멀티호스트/전용 엣지 수요). 이번 작업은
**additive** — `board-proxy` + Hypercorn TLS 로 "Caddy 불필요" 경로를 새로 추가할 뿐,
기존 caddy 모드·board-proxy 평문 모드는 그대로 동작(회귀 0).

## 1. 의존성

`pyproject.toml`:
- `uvicorn[standard]>=0.27` → **`hypercorn>=0.16`**
- Hypercorn 트랜지티브(h11·h2·priority·wsproto·hpack·hyperframe)는 전부 순수 파이썬
  → on-prem 배포 제약 부합. `[standard]`(uvloop·httptools 등 C확장) 제거로 빌드 의존↓.
- uvloop는 선택: Hypercorn도 `config.worker_class`/이벤트루프 커스터마이즈 가능하나
  기본 asyncio로 충분(현재도 uvicorn 기본 루프 특별 튜닝 없음).

## 2. 서버 기동부 (`app.py:main()`)

기존:
```python
uvicorn.run(create_app(config), host=host, port=port,
            log_config=build_log_config(config.log_file))
```

신규:
```python
import asyncio
from hypercorn.asyncio import serve
from hypercorn.config import Config as HConfig

hcfg = HConfig()
hcfg.bind = [f"{host}:{port}"]
cert = os.environ.get("AGENT_BOARD_TLS_CERT")
key = os.environ.get("AGENT_BOARD_TLS_KEY")
if cert and key:
    hcfg.certfile = cert
    hcfg.keyfile = key
    # ALPN 기본 ["h2","http/1.1"] → 브라우저가 h2 협상. 평문(cert 없음)이면 h1.
hcfg.accesslog = str(config.log_file)   # 회전 파일은 logconfig_dict 로(§4)
hcfg.errorlog = "-"                      # stderr
hcfg.logconfig_dict = build_log_config(config.log_file)  # hypercorn 로거명으로 재작성
asyncio.run(serve(create_app(config), hcfg))
```

- **TLS 판정 = cert&key 둘 다 존재.** 없으면 지금과 동일한 평문 h1(회귀 0).
- Hypercorn `serve()`는 SIGINT/SIGTERM 그레이스풀 종료를 기본 설치 → 기존 동작 유지.
- 싱글턴 락(`lock_fd`)·바인드 정책(`enforce_bind_policy`)·포트 선택은 그대로.

## 3. 바인드 정책 갱신 (`enforce_bind_policy`)

현재: `board-proxy` + 비-loopback = 인증 없음 → 거부(단, `ALLOW_UNAUTH_LAN=1`).
- TLS만으로는 **인증이 생기지 않는다**(암호화≠인증). 따라서 정책은 **불변** —
  board-proxy 비-loopback 노출은 여전히 auth 부재. (컨트롤플레인 auth는 이번 범위 밖;
  필요 시 후속 작업 `AGENT_BOARD_BASIC_AUTH` 미들웨어로.)
- 문서에 명시: "Hypercorn TLS는 도청 방지용. LAN/인터넷 노출 시 인증은 caddy 모드
  또는 향후 컨트롤플레인 auth 필요."

## 4. 로깅 (`build_log_config`)

uvicorn 로거명(`uvicorn`/`uvicorn.access`/`uvicorn.error`) + `uvicorn.logging`
포매터 → Hypercorn은 `hypercorn.access`/`hypercorn.error` 로거 + 표준 logging.
- `build_log_config`를 dictConfig로 재작성: 회전 파일 핸들러(`RotatingFileHandler`,
  access 폴링 소음 격리)는 유지, 로거명만 `hypercorn.*`로. 포매터는 표준 `logging.Formatter`
  (uvicorn 포매터 의존 제거).
- access 로그 포맷: Hypercorn 기본 `%(h)s %(r)s %(s)s ...` 사용(현재와 필드 유사).

## 5. h2 탐지 → 탭 가드 (핵심)

현재: 프런트 `app.js`가 `/api/gateway`의 `gateway==="caddy"`일 때만 가드 해제(442행).
Hypercorn h2로도 해제돼야 하므로 **전송 기반 신호**로 전환:
- `GET /api/gateway` 응답에 `h2: bool` 추가:
  `h2 = (config.gateway == "caddy") or bool(cert&key)`  ← board가 직접 h2 서빙 중인지.
  (main에서 TLS 여부를 app.state에 실어 endpoint가 읽음.)
- `app.js`: 가드 조건을 `gatewayMode !== "caddy"` → `!h2Capable`로 변경.
  하위호환: `h2` 필드 없으면(구버전) 기존 `gateway==="caddy"`로 폴백.
- `gateway_banner`/기동 로그: TLS 활성 시 `board-proxy (TLS, h2)` 표기.

## 6. SSE / 프록시 h2 검증 (수동 필수)

h2는 스트림 멀티플렉스라 SSE(`/api/events`, `/s/<id>/api/stream`)가 **더** 유리하나
청크 플러시·flow-control을 실측해야 한다:
- 자체서명 인증서 생성 → `AGENT_BOARD_TLS_CERT/KEY`로 기동.
- `curl -k --http2 https://127.0.0.1:PORT/api/version` → `HTTP/2 200` 확인.
- 브라우저로 글 열기 → `/s/<id>` 프록시·SSE 실시간 갱신 확인, 탭 6개+ 열어 가드
  해제(연결 1개 멀티플렉스) 확인.
- board-proxy StreamingResponse가 h2에서 `more_body` 청크로 흐르는지(버퍼링 없음).

## 7. 테스트

- `test_app.py`: `/api/gateway`가 `h2` 필드 반환(TLS on/off·caddy 케이스).
- 신규 `test_server_launch.py`(순수 함수 단위): main의 hcfg 구성을 헬퍼로 분리해
  (`build_hypercorn_config(config, host, port, cert, key)`) cert/key 유무 → certfile/keyfile
  ·bind·logconfig 매핑 검증(serve는 호출 안 함).
- `build_log_config`: hypercorn 로거명·회전 핸들러 존재 단위 검증.
- 회귀: 전체 스위트(현재 364) green + ruff + `node --check app.js`.

## 8. 문서

- `README.md`: 게이트웨이 표에 "board-proxy + TLS = Caddy 없이 h2" 경로 추가,
  `AGENT_BOARD_TLS_CERT/KEY` env 행 추가, caddy 섹션을 "선택(전용 엣지/멀티호스트)"로
  리포지셔닝. TLS≠인증 경고 명시.
- `docs/DESIGN.md` §9: 데이터플레인/서버 계층 갱신(ASGI=Hypercorn, h2 근거).
- `CHANGELOG.md` 1.28.0.
- `deploy/`: 자체서명 인증서 생성 one-liner + systemd/launchd 예시에 TLS env.

## 9. 버전 / 릴리스

- MINOR: `1.27.0 → 1.28.0` (하위호환 기능 추가; 서버 교체지만 기본 평문 h1 동작 불변).
- 커밋 1개(코드+테스트+문서) → 태그 v1.28.0 → wheel 빌드 → 격리 venv sanity
  (hypercorn 포함 설치·기동 스모크) → gh release → pycache 정리.

## 10. 리스크 / 롤백

- **리스크**: (a) SSE가 h2에서 예기치 않게 버퍼링 → §6 수동검증에서 조기 발견.
  (b) hypercorn 로그 포맷 차이로 access 로그 파서(있다면) 깨짐 → 회전 파일만 쓰므로 영향 적음.
  (c) Windows 등 플랫폼 signal 차이 → 대상은 macOS/Linux(현 배포)라 무영향.
- **롤백**: 서버 교체가 `main()`+`build_log_config`+pyproject 3곳에 국소화 →
  되돌리기 쉬움. caddy 모드는 안전망으로 상존.

## 11. 구현 순서 (핸드오프 체크리스트)

1. [ ] pyproject deps 스왑 + `pip install -e .`(hypercorn 확보)
2. [ ] `build_hypercorn_config` 헬퍼 + `main()` 교체
3. [ ] `build_log_config` hypercorn 로거명으로 재작성
4. [ ] `/api/gateway` h2 필드 + app.state TLS 플래그 + `gateway_banner` 갱신
5. [ ] `app.js` 가드 조건 h2 기반 + 폴백
6. [ ] 테스트(§7) 추가 + 전체 green + ruff + node --check
7. [ ] 자체서명 인증서로 §6 수동 검증(curl --http2 + 브라우저 SSE/프록시)
8. [ ] 문서(§8) + 버전 1.28.0
9. [ ] 커밋·태그·wheel·격리 sanity·gh release·pycache 정리
