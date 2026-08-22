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


def test_readme_cluster_examples_actually_parse():
    """Every `cluster.yml` block in the README goes through the real parser.

    A config example is copied, not read. H6 shipped one that named a live host;
    this guards the other half — an example that is merely WRONG, which a reader
    discovers as a backend error on their first start rather than as a typo in
    a doc. Parsed and validated with the same functions the backend uses, so
    "it looked right" cannot be the standard.
    """
    import sys

    for path in (ROOT / "backend" / "src", ROOT / "common" / "src"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from spark_dash_backend.cluster import parse_cluster, validate_cluster

    readme = (ROOT / "README.md").read_text()
    blocks = re.findall(r"```yaml\n(# central/cluster/cluster\.yml\n.*?)```", readme, re.S)
    assert blocks, "no cluster.yml example found in the README — did it move?"

    for block in blocks:
        nodes = parse_cluster(block)
        validate_cluster(nodes)
        assert nodes, "a cluster.yml example parses to no nodes at all"
        for node in nodes:
            # The placeholder rule from H6 applies to examples a reader copies:
            # never a real address, and never the maintainer's.
            assert not re.match(r"^192\.168\.50\.", node.host), (
                f"README example points at {node.host}, a real address on the "
                "maintainer's LAN"
            )
