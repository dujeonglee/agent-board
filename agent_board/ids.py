"""Post id generation.

``post_id`` is one value playing three roles, which is what constrains it:

1. the ``posts`` primary key,
2. the URL path segment of the instance route (``/s/<post_id>/*``, and the
   spawned agent-cli's ``--base-path``),
3. the workspace directory name — ``Config.workspace_for`` derives it as
   ``<workspaces_root>/<post_id>`` and never stores it, which is what keeps the
   board free of a user-supplied path (no injection surface, no shared-workspace
   collisions).

It used to be ``uuid.uuid4().hex`` — 32 characters. That was fine for (1) and
(2) but bad for (3): the directory name told you nothing about which post it
belonged to, and the absolute path rode along in the agent's context on every
file operation (a path is re-fed with each turn's action_input, and accumulates
in the compaction file list), where 32 characters of hex tokenize poorly.

So the id is now SHORT and RANDOM:

- **lowercase only** — macOS/Windows filesystems are case-insensitive, so a
  mixed-case alphabet could produce two ids that collide as directories,
- **Crockford base32** minus ``i``/``l``/``o``/``u`` — no glyph pairs a human
  can confuse when matching a directory against the id shown on a post card,
  and dropping ``u`` keeps accidental words out,
- **``secrets``, not ``random``** — with the default ``board-proxy`` gateway the
  ``/s/<id>`` route carries no authentication, so the id is the only thing
  standing between a LAN peer and an instance. It must not be predictable from
  other ids.

ENTROPY, stated plainly: 32**6 ≈ 1.07e9 (~30 bits), down from uuid4's 122. That
is a deliberate trade for a short path and is adequate ONLY because the board is
meant to run on localhost or behind ``caddy_basic_auth`` (which authenticates
each ``/s/<id>`` route independently of the id). If the board is ever exposed
directly, raise :data:`ID_LENGTH` — every consumer treats the id as an opaque
string, so nothing else has to change.
"""

from __future__ import annotations

import secrets

#: Crockford base32, lowercase, minus the confusable set (i, l, o, u).
ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"

#: Characters per id. See the module docstring on entropy before lowering it.
ID_LENGTH = 6


def new_post_id() -> str:
    """A fresh candidate id. Uniqueness is NOT guaranteed here — the caller
    allocates against the live board (see ``store.create_post``), because the id
    must be unique against BOTH the ``posts`` table and the workspaces directory
    (an orphaned directory from a half-finished delete must not be reused)."""
    return "".join(secrets.choice(ALPHABET) for _ in range(ID_LENGTH))
