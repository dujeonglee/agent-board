"""Board control-plane auth (agent_board/auth.py) — default-deny chokepoint.

핵심 회귀:
- 토큰 미설정(로컬 기본) → 미들웨어 없음 → 모든 경로 200 (표면 무변화)
- 토큰 설정 → /api/* 는 무인증 401 (fail-closed), ?token= / 쿠키로 통과
- /s/<id> 방·UI 셸·static 은 board-auth 면제(인스턴스 토큰이 방을 지킴)
- ?token= 성공은 abt 쿠키를 설치(HttpOnly·SameSite) + Referrer-Policy
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from agent_board import auth


def _app(token: str) -> FastAPI:
    app = FastAPI()
    if token:
        app.add_middleware(auth.AuthMiddleware, token=token)

    @app.get("/")
    async def index():
        return JSONResponse({"shell": True})

    @app.get("/api/posts")
    async def posts():
        return JSONResponse({"posts": []})

    @app.get("/api/health")
    async def health():
        return JSONResponse({"ok": True})

    @app.get("/s/{pid}/api/health")
    async def room(pid: str):
        return JSONResponse({"room": pid})

    return app


class TestNeedsAuth:
    def test_api_protected(self):
        assert auth.needs_auth("/api/posts")
        assert auth.needs_auth("/api/events")

    def test_health_public(self):
        assert not auth.needs_auth("/api/health")

    @pytest.mark.parametrize(
        "p", ["/", "/static/app.js", "/s/abc/api/health", "/admin"]
    )
    def test_non_api_never_gated(self, p):
        assert not auth.needs_auth(p)


class TestDisabled:
    """No token configured → middleware not installed → nothing gated."""

    def test_all_open(self):
        c = TestClient(_app(""))
        assert c.get("/api/posts").status_code == 200
        assert c.get("/").status_code == 200


class TestEnabled:
    def setup_method(self):
        self.c = TestClient(_app("sesame"))

    def test_api_denied_without_token(self):
        r = self.c.get("/api/posts")
        assert r.status_code == 401

    def test_api_ok_with_query_token(self):
        r = self.c.get("/api/posts?token=sesame")
        assert r.status_code == 200

    def test_api_denied_with_wrong_token(self):
        assert self.c.get("/api/posts?token=nope").status_code == 401

    def test_shell_and_room_exempt(self):
        # UI shell + /s/<id> room are not /api → reachable without board token
        assert self.c.get("/").status_code == 200
        assert self.c.get("/s/xyz/api/health").status_code == 200

    def test_health_exempt(self):
        assert self.c.get("/api/health").status_code == 200

    def test_query_token_sets_cookie(self):
        # fresh client so no cookie yet; bootstrap ?token= installs abt cookie
        c = TestClient(_app("sesame"))
        r = c.get("/api/posts?token=sesame")
        setc = r.headers.get("set-cookie", "")
        assert "abt=sesame" in setc and "HttpOnly" in setc
        assert "SameSite=Strict" in setc
        # cookie now authenticates subsequent requests without the query token
        assert c.get("/api/posts").status_code == 200

    def test_referrer_policy_on_response(self):
        r = self.c.get("/api/posts?token=sesame")
        assert r.headers.get("referrer-policy") == "no-referrer"


class TestTokenOk:
    def test_constant_time_semantics(self):
        assert auth.token_ok("abc", "abc")
        assert not auth.token_ok("abc", "abd")
        assert not auth.token_ok("abc", None)
        assert not auth.token_ok("", "abc")  # unconfigured never matches
