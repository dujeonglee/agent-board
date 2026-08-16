"""Board control-plane auth — default-deny ASGI chokepoint (mirrors agent-cli).

The board's own control plane (``/api/*`` — spawn/kill/delete/admin/list, the
live SSE stream) has no auth of its own; historically it was safe only because
board-proxy refused any non-loopback bind. This module adds a single default-deny
enforcer so the board can be exposed (LAN/internet) over Hypercorn TLS+h2 without
Caddy in front.

Model (see docs/hypercorn-embed-plan.md §Auth):
- **Only ``/api/*`` is guarded.** ``/``, ``/static/*``, ``/admin`` (HTML shells,
  no secrets) and ``/s/<id>/*`` are not ``/api`` → naturally exempt. The room
  content under ``/s/<id>`` is guarded by the INSTANCE's own token (the board
  proxies ``/s/<id>/?token=<instance_token>`` through), so board-auth deliberately
  does NOT gate it — that keeps share-links working and separates "manage the
  board" from "enter this room".
- A request authenticates via a valid ``abt`` cookie or a valid ``?token=`` (the
  bootstrap URL the operator opens once, or a curl/tool call). A valid ``?token=``
  also Sets the ``abt`` cookie, so the browser authenticates by cookie thereafter
  (and the SSE stream, which cannot send headers, rides the cookie) and the token
  leaves per-request URLs after one hop.
- Auth is enabled iff a token is configured (``config.auth_token``). When it is
  "", the middleware is not installed at all → zero-config loopback use unchanged.
- Every response carries ``Referrer-Policy: no-referrer`` so a token that did
  appear in a URL never leaks via the ``Referer`` header.
"""

from __future__ import annotations

import secrets

AUTH_COOKIE = "abt"  # agent-board token cookie (distinct from instance "act")


def token_ok(configured: str, candidate: str | None) -> bool:
    """Constant-time compare against the configured token (avoids a LAN timing
    side-channel). False when either side is empty/None."""
    if not configured or candidate is None:
        return False
    return secrets.compare_digest(candidate, configured)


def is_public_path(path: str) -> bool:
    """``/api`` paths served WITHOUT auth: only the liveness probe. Everything
    else under ``/api`` is default-deny."""
    return path == "/api/health"


def needs_auth(path: str) -> bool:
    """Default-deny: any ``/api`` path that is not explicitly public requires
    auth. A newly added ``/api`` route is protected by construction. Non-``/api``
    paths (UI shell, static assets, ``/s/<id>`` proxy) are never gated here."""
    return path.startswith("/api") and not is_public_path(path)


def _query_token(query_string: bytes) -> str | None:
    from urllib.parse import parse_qsl

    for k, v in parse_qsl(query_string.decode(errors="ignore"), keep_blank_values=True):
        if k == "token":
            return v
    return None


def _cookie_value(scope, name: str) -> str | None:
    from http.cookies import SimpleCookie

    for k, v in scope.get("headers", []):
        if k == b"cookie":
            jar = SimpleCookie()
            try:
                jar.load(v.decode("latin-1"))
            except Exception:
                return None
            m = jar.get(name)
            return m.value if m else None
    return None


def build_auth_cookie(token: str, *, secure: bool) -> str:
    """``Set-Cookie`` for the board auth cookie, scoped to the board root.

    ``HttpOnly`` keeps JS (hence XSS) from reading the token; ``SameSite=Strict``
    closes CSRF; ``Secure`` is added ONLY on TLS — a Secure cookie is silently
    dropped over the plain-HTTP the board serves without a cert."""
    parts = [f"{AUTH_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


async def _send_401(send) -> None:
    body = b'{"detail":"invalid or missing token"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"referrer-policy", b"no-referrer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _is_https(scope) -> bool:
    return scope.get("scheme") == "https" or any(
        k == b"x-forwarded-proto" and v == b"https" for k, v in scope.get("headers", [])
    )


class AuthMiddleware:
    """Pure-ASGI default-deny enforcer. Installed only when a token is configured.

    Any protected path (:func:`needs_auth`) that is unauthenticated gets a 401
    HERE — the endpoint never runs, so endpoints carry no auth code and a newly
    added ``/api`` route is protected by construction (fail-closed). A valid
    ``?token=`` sets the cookie; every response gets ``Referrer-Policy:
    no-referrer``."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        qs = scope.get("query_string", b"")
        valid_cookie = token_ok(self.token, _cookie_value(scope, AUTH_COOKIE))
        valid_query = token_ok(self.token, _query_token(qs))
        authed = valid_cookie or valid_query

        if needs_auth(scope.get("path", "")) and not authed:
            await _send_401(send)
            return

        set_cookie = valid_query and not valid_cookie
        secure = _is_https(scope)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                from starlette.datastructures import MutableHeaders

                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers.append("Referrer-Policy", "no-referrer")
                if set_cookie:
                    headers.append(
                        "Set-Cookie", build_auth_cookie(self.token, secure=secure)
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)
