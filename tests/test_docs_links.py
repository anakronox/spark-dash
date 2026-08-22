"""Every cross-reference in the docs resolves.

Worth guarding for a repo about to be published: a stale anchor is invisible to
the person who wrote it and lands a reader on the top of a 4,900-line file.
Five were already broken when this was first run, all from headings that had
been renamed as their sections shipped.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = sorted(
    list(ROOT.glob("*.md"))
    + list((ROOT / "docs").glob("*.md"))
    + list((ROOT / "central").glob("*.md"))
    + list((ROOT / "node").glob("*.md"))
)


def slug(heading: str) -> str:
    """GitHub's anchor algorithm: lowercase, drop punctuation, then replace
    EACH space with a hyphen.

    Not each RUN of spaces — and getting that wrong is not a small error. These
    headings use ` — ` liberally; the em dash is punctuation, so it vanishes
    and leaves two spaces, which become `--`. A version of this that collapsed
    whitespace reported 48 of 65 links broken, including three that had just
    been written by hand against the real anchors. Check the checker first.
    """
    s = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return s.replace(" ", "-")


def headings_of(path: Path) -> set[str]:
    return {slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.+)$", path.read_text(), re.M)}


def test_there_are_docs_to_check():
    """A link checker over an empty file list passes vacuously."""
    assert len(DOCS) >= 5, f"only found {[d.name for d in DOCS]}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_relative_links_point_at_files_that_exist(doc):
    broken = []
    for text, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", doc.read_text()):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path = target.split("#")[0]
        if path and not (doc.parent / path).resolve().exists():
            broken.append(f"[{text}]({target})")
    assert not broken, f"{doc.name} links to files that do not exist: {broken}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_anchors_point_at_headings_that_exist(doc):
    cache: dict[Path, set[str]] = {}
    broken = []
    for text, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", doc.read_text()):
        if "#" not in target or target.startswith(("http://", "https://")):
            continue
        path, _, anchor = target.partition("#")
        other = (doc.parent / path).resolve() if path else doc
        if not other.exists() or other.suffix != ".md":
            continue
        if other not in cache:
            cache[other] = headings_of(other)
        if anchor not in cache[other]:
            broken.append(f"[{text}]({target})")
    assert not broken, f"{doc.name} has anchors with no matching heading: {broken}"
