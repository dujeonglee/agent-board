"""Runtime configuration.

All paths/knobs the board needs. ``workspace_for(post_id)`` is the single source
of truth for a post's workspace directory — always
``<workspaces_root>/<post_id>``, derived (never user-supplied).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    data_dir: Path  # board.db + runtime state live here
    workspaces_root: Path  # per-post workspaces: <root>/<post_id>
    agent_cli_bin: str = "agent-cli"  # spawn binary
    idle_timeout: int = 300  # --idle-timeout passed to spawned instances
    gateway: str = "board-proxy"  # board-proxy (v1) | caddy
    caddy_admin: str = "http://127.0.0.1:2019"
    # "username:bcrypt-hash" (from `caddy hash-password`). When set with
    # gateway=caddy, the board embeds basic_auth into EACH dynamic /s/<id>
    # route so a proxied instance can never be reached unauthenticated.
    caddy_basic_auth: str = ""
    # ── board-native TLS + auth (Caddy 없이 HTTPS/h2 + 컨트롤플레인 인증) ──
    # cert&key 둘 다 있으면 Hypercorn 이 TLS(→ ALPN h2) 로 서빙, 없으면 평문 h1.
    tls_cert: str = ""
    tls_key: str = ""
    # 컨트롤플레인(/api/*) default-deny 토큰. "" 이면 auth OFF(로컬 기본).
    # main() 이 비-loopback 바인드 시 자동생성·영속화해 채울 수 있다.
    auth_token: str = ""
    # agent-cli model registry the board reads to list selectable models.
    # (admin 페이지는 이 파일과 agent_cli_config_json 을 편집한다.)
    models_json: Path = Path.home() / ".agent-cli" / "models.json"
    agent_cli_config_json: Path = Path.home() / ".agent-cli" / "config.json"
    port_min: int = 50000
    port_max: int = 60000

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).resolve()
        self.workspaces_root = Path(self.workspaces_root).resolve()
        self.models_json = Path(self.models_json)
        self.agent_cli_config_json = Path(self.agent_cli_config_json)

    @property
    def tls_enabled(self) -> bool:
        """TLS on only when BOTH cert and key are configured (→ Hypercorn ALPN h2)."""
        return bool(self.tls_cert and self.tls_key)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "board.db"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "board.log"

    def workspace_for(self, post_id: str) -> Path:
        """The (derived) workspace directory for a post. Always under
        ``workspaces_root`` — the board owns it, so creation/deletion is safe
        and there is no path-injection surface."""
        return self.workspaces_root / post_id

    @classmethod
    def from_env(cls) -> Config:
        base = Path(os.environ.get("AGENT_BOARD_HOME", "./data")).resolve()
        return cls(
            data_dir=Path(os.environ.get("AGENT_BOARD_DATA", base)),
            workspaces_root=Path(
                os.environ.get("AGENT_BOARD_WORKSPACES", base / "workspaces")
            ),
            agent_cli_bin=os.environ.get("AGENT_BOARD_CLI", "agent-cli"),
            idle_timeout=int(os.environ.get("AGENT_BOARD_IDLE_TIMEOUT", "300")),
            gateway=os.environ.get("AGENT_BOARD_GATEWAY", "board-proxy"),
            caddy_admin=os.environ.get(
                "AGENT_BOARD_CADDY_ADMIN", "http://127.0.0.1:2019"
            ),
            caddy_basic_auth=os.environ.get("AGENT_BOARD_CADDY_BASIC_AUTH", ""),
            tls_cert=os.environ.get("AGENT_BOARD_TLS_CERT", ""),
            tls_key=os.environ.get("AGENT_BOARD_TLS_KEY", ""),
            auth_token=os.environ.get("AGENT_BOARD_AUTH_TOKEN", ""),
            models_json=Path(
                os.environ.get(
                    "AGENT_BOARD_MODELS_JSON",
                    Path.home() / ".agent-cli" / "models.json",
                )
            ),
            agent_cli_config_json=Path(
                os.environ.get(
                    "AGENT_BOARD_AGENT_CLI_CONFIG",
                    Path.home() / ".agent-cli" / "config.json",
                )
            ),
        )
