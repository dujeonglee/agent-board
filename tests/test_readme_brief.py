"""README 는 설치·사용 예시만 (v1.46.1) — 짧게, 버전 이력 없이, 링크·그림이 살아 있게.

레퍼런스는 docs/REFERENCE.md, 배포는 docs/DEPLOY.md 로 옮겼다. 이 가드는 README 가
다시 레퍼런스로 불어나거나, 옮긴 문서끼리의 링크·앵커가 끊기는 것을 막는다.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
IMAGES = ROOT / "docs" / "images"
LINKED_DOCS = [README, ROOT / "docs" / "REFERENCE.md", ROOT / "docs" / "DEPLOY.md"]

MAX_README_LINES = 150
MAX_IMAGE_BYTES = 1_000_000

_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
_FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)


def _strip_code(text: str) -> str:
    return _FENCE.sub("", text)


def github_anchors(text: str) -> set[str]:
    """GitHub 식 제목 앵커: 소문자화 → 글자·숫자·``-``·``_``·공백 외 제거 →
    공백을 ``-`` 로. 한글은 글자라 남는다. 같은 앵커가 다시 나오면 ``-1``, ``-2``."""
    seen: dict[str, int] = {}
    out: set[str] = set()
    for line in _strip_code(text).splitlines():
        m = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not m:
            continue
        title = re.sub(r"`", "", m.group(1)).strip().lower()
        slug = "".join(ch for ch in title if ch.isalnum() or ch in "-_ ")
        slug = slug.replace(" ", "-")
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    return out


def _relative_links(path: Path) -> list[str]:
    links = _LINK.findall(_strip_code(path.read_text(encoding="utf-8")))
    return [u for u in links if not re.match(r"^[a-z][a-z0-9+.-]*:", u)]


def test_readme_is_short():
    n = len(README.read_text(encoding="utf-8").splitlines())
    assert n <= MAX_README_LINES, f"README.md 가 {n}줄 — 상한 {MAX_README_LINES}"


def test_readme_has_no_version_history():
    # 요구사항의 agent-cli 권장 버전은 "≥ 10.x.y" 로 쓴다 — 앞에 v 가 붙은 꼴만 막는다.
    hits = re.findall(r"\bv\d+\.\d+\.\d+", README.read_text(encoding="utf-8"))
    assert not hits, f"README 에 버전 이력: {hits}"


def test_readme_images_exist_and_are_small():
    imgs = _IMAGE.findall(README.read_text(encoding="utf-8"))
    assert imgs, "README 에 그림이 없다"
    for rel in imgs:
        p = (ROOT / rel).resolve()
        assert p.parent == IMAGES.resolve(), f"그림은 docs/images/ 에: {rel}"
        assert p.is_file(), f"없는 그림: {rel}"
        assert p.stat().st_size <= MAX_IMAGE_BYTES, f"1 MB 넘는 그림: {rel}"


def test_no_unused_images():
    used = {
        (ROOT / rel).resolve().name
        for rel in _IMAGE.findall(README.read_text(encoding="utf-8"))
    }
    unused = sorted(
        p.name for p in IMAGES.iterdir() if p.is_file() and p.name not in used
    )
    assert not unused, f"README 가 쓰지 않는 그림: {unused}"


def test_relative_links_and_anchors_resolve():
    broken = []
    for doc in LINKED_DOCS:
        for link in _relative_links(doc):
            target, _, anchor = link.partition("#")
            dest = (doc.parent / target).resolve() if target else doc
            if not dest.exists():
                broken.append(f"{doc.name}: {link} (파일 없음)")
                continue
            if (
                anchor
                and dest.suffix == ".md"
                and anchor not in github_anchors(dest.read_text(encoding="utf-8"))
            ):
                broken.append(f"{doc.name}: {link} (앵커 없음)")
    assert not broken, "끊긴 링크:\n" + "\n".join(broken)


def test_anchor_rule():
    text = "# A b\n## 방 열기·재실행\n## `Open` it!\n## A b\n```\n# not a title\n```\n"
    assert github_anchors(text) == {"a-b", "방-열기재실행", "open-it", "a-b-1"}
