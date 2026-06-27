# agent-board

게시판형 웹 서비스 — **글 하나 = [agent-cli](https://github.com/dujeonglee/agent-cli)
세션 하나 + 워크스페이스 + 주제**. 글을 클릭하면 그 세션의 agent-cli web UI 에 접속한다.

- 인스턴스는 **온디맨드로 떴다가 idle 시 스스로 종료**(agent-cli `--idle-timeout`),
  다음 접속에 `--resume` 으로 재기동.
- agent-cli 는 **외부 CLI 로만 호출**하며 수정하지 않는다.

## 설계 문서
- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — 요구사항
- [docs/DESIGN.md](docs/DESIGN.md) — 모듈/API/DB/라이프사이클 설계

## agent-cli 선행 기능 (v4.13+ 필요)
`--idle-timeout` · 인스턴스 파일 `web.json` · `--trust-local` · `--base-path`

## 개발
```bash
pip install -e ".[dev]"
pytest
```
