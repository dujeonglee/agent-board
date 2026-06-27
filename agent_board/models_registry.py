"""List selectable models from agent-cli's ``models.json`` registry (DESIGN §8).

The board does NOT manage model definitions/keys — those live in agent-cli's
registry (``~/.agent-cli/models.json``, admin-managed). The board only reads the
ids to populate the new-post dropdown, stores the chosen id per post, and passes
it as ``--model <id>`` on spawn (agent-cli resolves provider/url/key itself).
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_MODELS_JSON = Path.home() / ".agent-cli" / "models.json"


def list_models(path: str | Path | None = None) -> list[dict]:
    """``[{id, provider, context_window}]`` sorted by id (case-insensitive).
    Empty list if the registry is missing/corrupt."""
    path = Path(path) if path else DEFAULT_MODELS_JSON
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        return []
    out = [
        {
            "id": mid,
            "provider": (meta or {}).get("provider"),
            "context_window": (meta or {}).get("context_window"),
        }
        for mid, meta in models.items()
    ]
    return sorted(out, key=lambda m: m["id"].lower())
