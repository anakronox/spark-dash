"""Inventory parsing.

Shared with Prometheus deliberately: two node lists would drift, and the
failure would be quiet — a node visible in history but missing from the live
view, or vice versa.
"""

from spark_dash_backend.inventory import Inventory, Node, parse_file_sd

REAL = """
- targets: ['192.168.50.61:9500']
  labels:
    node: gx10-1

- targets: ['192.168.50.62:9500']
  labels:
    node: gx10-2
"""


def test_parses_prometheus_file_sd():
    nodes = parse_file_sd(REAL)
    assert [n.node_id for n in nodes] == ["gx10-1", "gx10-2"]
    assert nodes[0].address == "192.168.50.61:9500"


def test_derives_urls():
    node = Node(node_id="gx10-1", address="192.168.50.61:9500")
    assert node.agent_url == "http://192.168.50.61:9500"
    assert node.snapshot_url == "http://192.168.50.61:9500/snapshot"


def test_multiple_targets_in_one_entry():
    nodes = parse_file_sd("- targets: ['a:9500', 'b:9500']\n  labels:\n    node: shared\n")
    # Second is dropped: a duplicate node id would double-count in aggregates.
    assert [n.node_id for n in nodes] == ["shared"]


def test_missing_node_label_falls_back_to_host():
    """A half-configured entry should still be usable rather than vanish."""
    nodes = parse_file_sd("- targets: ['192.168.50.61:9500']\n")
    assert nodes[0].node_id == "192.168.50.61"


def test_duplicate_node_ids_are_dropped():
    content = """
- targets: ['a:9500']
  labels: {node: dup}
- targets: ['b:9500']
  labels: {node: dup}
"""
    nodes = parse_file_sd(content)
    assert len(nodes) == 1
    assert nodes[0].address == "a:9500"


def test_malformed_entries_are_skipped_not_fatal():
    """One bad line must not blind the whole dashboard."""
    content = """
- targets: ['good:9500']
  labels: {node: good}
- "not a mapping"
- targets: "not a list"
  labels: {node: bad}
"""
    nodes = parse_file_sd(content)
    assert [n.node_id for n in nodes] == ["good"]


def test_invalid_yaml_yields_empty_not_exception():
    assert parse_file_sd("{[unclosed") == []


def test_empty_file():
    assert parse_file_sd("") == []
    assert parse_file_sd("[]") == []


def test_commented_out_nodes_are_ignored():
    """The shipped inventory has nodes 2 and 3 commented out."""
    content = """
- targets: ['192.168.50.61:9500']
  labels: {node: gx10-1}
# - targets: ['192.168.50.62:9500']
#   labels: {node: gx10-2}
"""
    assert [n.node_id for n in parse_file_sd(content)] == ["gx10-1"]


class TestInventoryCache:
    def test_reads_and_caches(self, tmp_path):
        path = tmp_path / "agents.yml"
        path.write_text(REAL)
        inv = Inventory(path, ttl_s=60.0)

        assert len(inv.nodes(now=0.0)) == 2
        path.write_text("- targets: ['c:9500']\n  labels: {node: gx10-3}\n")
        # Within TTL: still the cached view.
        assert len(inv.nodes(now=10.0)) == 2

    def test_rereads_after_ttl(self, tmp_path):
        """Adding a node must not require a backend restart."""
        path = tmp_path / "agents.yml"
        path.write_text("- targets: ['a:9500']\n  labels: {node: gx10-1}\n")
        inv = Inventory(path, ttl_s=30.0)
        assert len(inv.nodes(now=0.0)) == 1

        path.write_text(REAL)
        assert len(inv.nodes(now=31.0)) == 2

    def test_unreadable_file_keeps_previous_inventory(self, tmp_path):
        """A file briefly unreadable mid-edit must not drop every node."""
        path = tmp_path / "agents.yml"
        path.write_text(REAL)
        inv = Inventory(path, ttl_s=0.0)
        assert len(inv.nodes(now=0.0)) == 2

        path.unlink()
        assert len(inv.nodes(now=1.0)) == 2

    def test_missing_file_at_startup_is_empty_not_fatal(self, tmp_path):
        inv = Inventory(tmp_path / "nope.yml", ttl_s=0.0)
        assert inv.nodes(now=0.0) == []
