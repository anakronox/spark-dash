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


ALERT_FILES = sorted((ROOT / "central" / "config").glob("alerts*.yml"))

#: Docs that describe the CURRENT alert set to an operator. The roadmap is
#: deliberately excluded: it is a decision log, and naming a rule that was
#: renamed or retired is exactly its job -- `MemoryHighWithSwap` appears there
#: because measurement killed the theory behind it, which is a thing the record
#: should keep saying.
ALERT_DOCS = [ROOT / "central" / "README.md", ROOT / "node" / "README.md", ROOT / "README.md"]


def defined_alerts() -> set[str]:
    names: set[str] = set()
    for f in ALERT_FILES:
        names |= set(re.findall(r"^\s*- alert:\s*(\w+)", f.read_text(), re.M))
    return names


def test_alert_rules_exist_to_check():
    """Guarding against an empty set, which would pass every doc vacuously."""
    assert len(defined_alerts()) > 20, "found suspiciously few alert rules"


@pytest.mark.parametrize("doc", ALERT_DOCS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_documented_alert_names_still_exist(doc):
    """Alert names in operator docs go stale silently.

    `central/README.md` listed nine alerts as though that were the set; there
    are 34. One of the nine had been renamed, so the doc named a rule that did
    not exist — worse than omitting it, because a reader greps for it and
    concludes their own config is broken.
    """
    defined = defined_alerts()
    # Alert-shaped: two or more CamelCase words in backticks. `Prometheus` or
    # `NVML` do not match; `NodeAgentDown` does.
    named = {
        m for m in re.findall(r"`([A-Za-z]+)`", doc.read_text())
        if re.match(r"^(?:[A-Z][a-z0-9]+){2,}$", m)
    }
    stale = named - defined
    assert not stale, (
        f"{doc.name} names alert rules that are not in central/config/alerts*.yml: "
        f"{sorted(stale)}"
    )
