"""Packaging guard: non-.py 리소스가 package-data 에 덮여 있는지.

agent-cli 실측 교훈 이식 — editable install 은 소스 트리를 직접 읽어
리소스 누락을 가리고, pip wheel 설치에서만 터진다 (agent-cli 의
reviewer.md·woff2 누락 사고). 디스크의 모든 non-.py 리소스가
package-data 글롭에 매칭되지 않으면 실패.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # py3.10
    import tomli as tomllib  # type: ignore[no-redef]

_PKG = Path(__file__).resolve().parent.parent / "agent_board"
_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _patterns() -> list[str]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return data["tool"]["setuptools"]["package-data"]["agent_board"]


def _covered(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) for pat in patterns)


class TestPackageData:
    def test_all_static_resources_covered(self):
        patterns = _patterns()
        missing = []
        for f in _PKG.rglob("*"):
            if f.is_dir() or f.suffix == ".py" or "__pycache__" in f.parts:
                continue
            rel = str(f.relative_to(_PKG))
            if not _covered(rel, patterns):
                missing.append(rel)
        assert not missing, f"package-data 에 안 덮인 리소스: {missing}"

    def test_recursive_glob(self):
        # "static/*" 비재귀 회귀 방지 — 하위 디렉토리 자원도 커버되어야 함.
        assert _covered("static/fonts/hypothetical.woff2", _patterns())
