"""Node inventory.

SPARK_NODES is the single source of truth; the backend renders Prometheus's
scrape targets from it. Two independently-maintained node lists would drift,
and the failure would be quiet — a node visible in history but missing from the
live view, or the reverse.
"""

from spark_dash_backend.inventory import (
    Inventory,
    Node,
    parse_file_sd,
    parse_nodes_env,
    write_prometheus_targets,
)

FILE_SD = """
- targets: ['192.168.50.61:9500']
  labels:
    node: gx10-1
- targets: ['192.168.50.62:9500']
  labels:
    node: gx10-2
"""


class TestParseNodesEnv:
    def test_the_common_case(self):
        nodes = parse_nodes_env("gx10-1=192.168.50.61,gx10-2=192.168.50.62")
        assert [n.node_id for n in nodes] == ["gx10-1", "gx10-2"]
        assert nodes[0].address == "192.168.50.61:9500"
        assert nodes[0].node_exporter_address == "192.168.50.61:9100"

    def test_at_sign_separator(self):
        assert parse_nodes_env("gx10-1@192.168.50.61")[0].node_id == "gx10-1"

    def test_explicit_port_override(self):
        node = parse_nodes_env("gx10-1=192.168.50.61:9600")[0]
        assert node.address == "192.168.50.61:9600"
        # node_exporter port is independent of the agent port.
        assert node.node_exporter_address == "192.168.50.61:9100"

    def test_bare_host_derives_id(self):
        node = parse_nodes_env("192.168.50.61")[0]
        assert node.node_id == "192.168.50.61"

    def test_whitespace_is_tolerated(self):
        nodes = parse_nodes_env("  gx10-1 = 192.168.50.61 ,  gx10-2=192.168.50.62 ")
        assert [n.node_id for n in nodes] == ["gx10-1", "gx10-2"]
        assert nodes[0].host == "192.168.50.61"

    def test_empty_yields_nothing(self):
        assert parse_nodes_env("") == []
        assert parse_nodes_env("  ,  , ") == []

    def test_trailing_comma(self):
        assert len(parse_nodes_env("gx10-1=192.168.50.61,")) == 1

    def test_duplicate_ids_are_dropped(self):
        """A duplicate would double-count in cluster aggregates."""
        nodes = parse_nodes_env("dup=10.0.0.1,dup=10.0.0.2")
        assert len(nodes) == 1
        assert nodes[0].host == "10.0.0.1"

    def test_entry_with_no_host_is_skipped(self):
        nodes = parse_nodes_env("gx10-1=,gx10-2=192.168.50.62")
        assert [n.node_id for n in nodes] == ["gx10-2"]

    def test_hostnames_work_not_just_ips(self):
        node = parse_nodes_env("gx10-1=spark1.lan")[0]
        assert node.address == "spark1.lan:9500"

    def test_custom_default_ports(self):
        node = parse_nodes_env("a=h", agent_port=1234, node_exporter_port=5678)[0]
        assert node.address == "h:1234"
        assert node.node_exporter_address == "h:5678"

    def test_urls_derived(self):
        node = parse_nodes_env("gx10-1=192.168.50.61")[0]
        assert node.agent_url == "http://192.168.50.61:9500"
        assert node.snapshot_url == "http://192.168.50.61:9500/snapshot"


class TestRenderPrometheusTargets:
    def test_writes_both_target_files(self, tmp_path):
        nodes = parse_nodes_env("gx10-1=192.168.50.61,gx10-2=192.168.50.62")
        assert write_prometheus_targets(nodes, tmp_path) is True

        agents = (tmp_path / "agents.yml").read_text()
        exporters = (tmp_path / "node-exporters.yml").read_text()

        assert "192.168.50.61:9500" in agents
        assert "192.168.50.62:9500" in agents
        assert "192.168.50.61:9100" in exporters
        assert "GENERATED FILE" in agents

    def test_rendered_file_parses_back_to_the_same_nodes(self, tmp_path):
        """Round-trip: what we write must be what Prometheus (and our own
        fallback parser) reads."""
        nodes = parse_nodes_env("gx10-1=192.168.50.61,gx10-2=192.168.50.62")
        write_prometheus_targets(nodes, tmp_path)

        reparsed = parse_file_sd((tmp_path / "agents.yml").read_text())
        assert [n.node_id for n in reparsed] == ["gx10-1", "gx10-2"]
        assert [n.address for n in reparsed] == [n.address for n in nodes]

    def test_node_label_is_written_explicitly(self, tmp_path):
        """A DOWN target still needs an identity, or a node that never came up
        would be invisible rather than visibly missing."""
        write_prometheus_targets(parse_nodes_env("gx10-1=192.168.50.61"), tmp_path)
        assert "node: gx10-1" in (tmp_path / "agents.yml").read_text()

    def test_unchanged_content_is_not_rewritten(self, tmp_path):
        """Avoids churning mtimes Prometheus watches."""
        nodes = parse_nodes_env("gx10-1=192.168.50.61")
        assert write_prometheus_targets(nodes, tmp_path) is True
        assert write_prometheus_targets(nodes, tmp_path) is False

    def test_changed_inventory_rewrites(self, tmp_path):
        write_prometheus_targets(parse_nodes_env("gx10-1=192.168.50.61"), tmp_path)
        changed = write_prometheus_targets(
            parse_nodes_env("gx10-1=192.168.50.61,gx10-2=192.168.50.62"), tmp_path
        )
        assert changed is True
        assert "192.168.50.62" in (tmp_path / "agents.yml").read_text()

    def test_unwritable_dir_is_not_fatal(self, tmp_path):
        """The live view still works; Prometheus keeps its previous targets."""
        assert write_prometheus_targets(parse_nodes_env("a=h"), tmp_path / "nope") is False


class TestInventory:
    def test_env_is_preferred_over_file(self, tmp_path):
        targets = tmp_path / "agents.yml"
        targets.write_text(FILE_SD)

        inv = Inventory(nodes_env="envnode=10.0.0.9", targets_file=targets)
        assert [n.node_id for n in inv.nodes(now=0.0)] == ["envnode"]
        assert inv.source == "env"

    def test_falls_back_to_file_when_env_unset(self, tmp_path):
        targets = tmp_path / "agents.yml"
        targets.write_text(FILE_SD)

        inv = Inventory(nodes_env="", targets_file=targets)
        assert [n.node_id for n in inv.nodes(now=0.0)] == ["gx10-1", "gx10-2"]
        assert inv.source == "file"

    def test_caches_within_ttl_and_rereads_after(self, tmp_path):
        targets = tmp_path / "agents.yml"
        targets.write_text("- targets: ['a:9500']\n  labels: {node: one}\n")
        inv = Inventory(nodes_env="", targets_file=targets, ttl_s=30.0)

        assert len(inv.nodes(now=0.0)) == 1
        targets.write_text(FILE_SD)
        assert len(inv.nodes(now=10.0)) == 1
        assert len(inv.nodes(now=31.0)) == 2

    def test_unreadable_file_keeps_previous_inventory(self, tmp_path):
        targets = tmp_path / "agents.yml"
        targets.write_text(FILE_SD)
        inv = Inventory(nodes_env="", targets_file=targets, ttl_s=0.0)
        assert len(inv.nodes(now=0.0)) == 2

        targets.unlink()
        assert len(inv.nodes(now=1.0)) == 2

    def test_no_source_at_all_is_empty_not_fatal(self):
        assert Inventory(nodes_env="", targets_file=None).nodes(now=0.0) == []

    def test_sync_writes_targets(self, tmp_path):
        inv = Inventory(
            nodes_env="gx10-1=192.168.50.61,gx10-2=192.168.50.62",
            prometheus_targets_dir=tmp_path,
        )
        assert inv.sync_prometheus_targets() is True
        assert "192.168.50.62:9500" in (tmp_path / "agents.yml").read_text()

    def test_sync_is_a_noop_without_a_target_dir(self):
        inv = Inventory(nodes_env="a=h", prometheus_targets_dir=None)
        assert inv.sync_prometheus_targets() is False


def test_node_equality_is_by_value():
    """Nodes are compared when detecting inventory changes."""
    assert Node("a", "h") == Node("a", "h")
    assert Node("a", "h") != Node("a", "h2")


class TestParseFileSd:
    def test_malformed_entries_are_skipped_not_fatal(self):
        content = """
- targets: ['good:9500']
  labels: {node: good}
- "not a mapping"
- targets: "not a list"
  labels: {node: bad}
"""
        assert [n.node_id for n in parse_file_sd(content)] == ["good"]

    def test_invalid_yaml_yields_empty(self):
        assert parse_file_sd("{[unclosed") == []

    def test_missing_node_label_falls_back_to_host(self):
        assert parse_file_sd("- targets: ['192.168.50.61:9500']\n")[0].node_id == "192.168.50.61"


class TestGroups:
    """Not every node is part of a cluster. Grouping is what keeps capacity
    arithmetic honest: memory pools WITHIN a group (clustered nodes do
    distributed inference, so a model can span them) and never across groups.
    """

    def test_group_prefix(self):
        nodes = parse_nodes_env("solo=10.0.0.1,pair/a=10.0.0.2,pair/b=10.0.0.3")
        assert [n.group for n in nodes] == [None, "pair", "pair"]
        assert [n.node_id for n in nodes] == ["solo", "a", "b"]

    def test_ungrouped_node_is_standalone(self):
        node = parse_nodes_env("solo=10.0.0.1")[0]
        assert node.standalone is True
        # A standalone node is a group of one, so callers aggregate uniformly.
        assert node.group_key == "solo"

    def test_grouped_nodes_share_a_key(self):
        nodes = parse_nodes_env("pair/a=10.0.0.2,pair/b=10.0.0.3")
        assert {n.group_key for n in nodes} == {"pair"}
        assert all(not n.standalone for n in nodes)

    def test_group_prefix_on_a_bare_host(self):
        node = parse_nodes_env("pair/10.0.0.2")[0]
        assert node.group == "pair"
        assert node.node_id == "10.0.0.2"

    def test_group_with_explicit_port(self):
        node = parse_nodes_env("pair/a=10.0.0.2:9600")[0]
        assert node.group == "pair"
        assert node.address == "10.0.0.2:9600"

    def test_empty_group_prefix_is_treated_as_ungrouped(self):
        assert parse_nodes_env("/a=10.0.0.2")[0].group is None

    def test_group_label_is_written_to_prometheus_targets(self, tmp_path):
        """So history aggregates the same way the live view does: sum by
        (group) is meaningful, a bare sum is not."""
        nodes = parse_nodes_env("solo=10.0.0.1,pair/a=10.0.0.2")
        write_prometheus_targets(nodes, tmp_path)
        agents = (tmp_path / "agents.yml").read_text()

        assert "group: pair" in agents
        # The standalone node gets no group label rather than a placeholder,
        # so `group=""` never becomes a meaningless bucket in PromQL.
        assert agents.count("group:") == 1
