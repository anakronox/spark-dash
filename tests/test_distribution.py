"""No personal values in the files a stranger copies.

H's rule, and the one H1a's audit table got wrong by omission: a value is a
problem when a reader PASTES it, not when they read it. Commented lines and
prose are examples and stay; anything uncommented in a template is config
somebody will actually run.

`central/cluster.yml.example` broke this and nothing caught it. It is the first
file the quickstart tells you to copy, and its active block named a real node
at a real address -- so a clean-clone walk on 2026-08-22 came up monitoring the
maintainer's `sparky` and reported its agent version, which is exactly the
failure this file exists to prevent.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Templates a reader copies, plus the two compose files whose defaults run
#: unedited on a fresh clone.
TEMPLATES = sorted(
    [p for p in (ROOT / "central").glob("*.example")]
    + [p for p in (ROOT / "node").glob("*.example")]
    + [ROOT / "central" / "compose.yaml", ROOT / "node" / "compose.yaml"]
)

#: RFC1918 is not the test — reachability is not the point. The point is that a
#: pasted value silently describes somebody else's deployment.
PERSONAL = {
    "the maintainer's LAN": re.compile(r"192\.168\.50\.\d+"),
    "the maintainer's registry": re.compile(r"forgejo\.indielab\.tech"),
    "the maintainer's node names": re.compile(r"\b(sparky|sparketa|sparkjr|danflashes)\b"),
}


def uncommented(text: str) -> str:
    """Config a reader would actually run: comments stripped, both the
    whole-line and trailing kinds. Getting this wrong in the lax direction
    would let the guard pass on a live value hiding after a `#`."""
    out = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0]
        if stripped.strip():
            out.append(stripped)
    return "\n".join(out)


def test_there_are_templates_to_check():
    """A guard over an empty list passes vacuously — this suite has been bitten
    by that shape before."""
    assert len(TEMPLATES) >= 4, f"only found {[p.name for p in TEMPLATES]}"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_personal_values_in_what_a_stranger_copies(path):
    body = uncommented(path.read_text())
    for what, pattern in PERSONAL.items():
        hit = pattern.search(body)
        assert hit is None, (
            f"{path.relative_to(ROOT)} has {what} in a line that is not a comment: "
            f"{hit.group(0)!r} — a reader copies this file and runs it"
        )


def test_the_placeholder_fails_loudly_rather_than_resolving():
    """`.invalid` is reserved by RFC 2606 and can never resolve, so a copied-
    but-unedited file reports the node unreachable instead of quietly pointing
    at whatever answers on the reader's own LAN."""
    for name in ("cluster.yml.example", "cluster.yml.single-host.example"):
        body = uncommented((ROOT / "central" / name).read_text())
        host = re.search(r"host:\s*(\S+)", body)
        assert host, f"{name} has no host line"
        assert host.group(1).endswith(".invalid"), (
            f"{name} ships host={host.group(1)!r} — a plausible-looking address "
            "points an unedited config at a real machine on the reader's LAN"
        )
