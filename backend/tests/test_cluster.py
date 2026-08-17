"""The cluster definition parser.

The point of this file is that a node's runtimes stop living in that node's own
`.env`, which is what let the per-node stack become identical everywhere. So
the tests care most about two things: that ports resolve against the right
host, and that a malformed file fails loudly rather than degrading to "no
runtimes" — which would leave every node reporting no models with nothing
saying why.
"""

import pytest
from spark_dash_backend.cluster import (
    ClusterConfigError,
    load_cluster,
    parse_cluster,
)


def by_id(nodes):
    """The parser returns an ordered list; most assertions want a lookup."""
    return {n.node_id: n for n in nodes}

GX10 = """
nodes:
  - id: sparky
    host: 192.168.50.61
    runtimes:
      llama_routers:
        - port: 8001
          scrape_metrics: true
        - port: 8108
      vllm:
        - 8120
"""


class TestParsing:
    def test_ports_resolve_against_the_node_host(self):
        """The whole point: the node stack carries no host-specific config, so
        a port plus the node's host is what makes it identical everywhere."""
        cfg = by_id(parse_cluster(GX10))["sparky"].runtimes
        assert [r.url for r in cfg.llama_routers] == [
            "http://192.168.50.61:8001",
            "http://192.168.50.61:8108",
        ]

    def test_vllm_ports_get_the_metrics_path(self):
        """vLLM's Prometheus endpoint is conventionally /metrics; making every
        entry spell that out would be noise."""
        assert by_id(parse_cluster(GX10))["sparky"].runtimes.vllm == ["http://192.168.50.61:8120/metrics"]

    def test_scrape_metrics_is_per_router(self):
        """Opting one router in must not opt in the one hosting 70B models —
        the blast radius of waking a model differs by router."""
        routers = by_id(parse_cluster(GX10))["sparky"].runtimes.llama_routers
        assert routers[0].scrape_metrics is True
        assert routers[1].scrape_metrics is False

    def test_explicit_url_overrides_the_shorthand(self):
        """The escape hatch: a runtime that is not on the node's own address."""
        cfg = by_id(parse_cluster("""
nodes:
  - id: n
    host: 10.0.0.1
    runtimes:
      llama_routers:
        - url: http://elsewhere:9999
"""))["n"].runtimes
        assert cfg.llama_routers[0].url == "http://elsewhere:9999"

    def test_node_with_no_runtimes(self):
        """Normal: a node may serve neither, and must not fail to parse."""
        cfg = by_id(parse_cluster("nodes:\n  - id: n\n    host: 10.0.0.1\n"))["n"].runtimes
        assert cfg.llama_routers == [] and cfg.vllm == []

    def test_several_nodes_keep_their_own_hosts(self):
        cfg = by_id(parse_cluster("""
nodes:
  - id: a
    host: 10.0.0.1
    runtimes: {llama_routers: [{port: 8001}]}
  - id: b
    host: 10.0.0.2
    runtimes: {llama_routers: [{port: 8001}]}
"""))
        assert cfg["a"].runtimes.llama_routers[0].url == "http://10.0.0.1:8001"
        assert cfg["b"].runtimes.llama_routers[0].url == "http://10.0.0.2:8001"

    def test_unparseable_entry_is_skipped_not_fatal(self):
        """One bad runtime entry should not cost the node its other runtimes."""
        cfg = by_id(parse_cluster("""
nodes:
  - id: n
    host: 10.0.0.1
    runtimes:
      llama_routers:
        - {}
        - port: 8001
"""))["n"].runtimes
        assert [r.url for r in cfg.llama_routers] == ["http://10.0.0.1:8001"]


class TestFailsLoudly:
    """A malformed file must raise. Degrading to "no runtimes" would leave
    every node reporting no models, which reads as an outage rather than a
    typo — and the dashboard exists to make that distinction."""

    def test_invalid_yaml(self):
        with pytest.raises(ClusterConfigError):
            parse_cluster("nodes: [unclosed")

    def test_missing_nodes_key(self):
        with pytest.raises(ClusterConfigError):
            parse_cluster("something_else: 1")

    def test_node_without_id(self):
        with pytest.raises(ClusterConfigError, match="missing its `id`"):
            parse_cluster("nodes:\n  - host: 10.0.0.1\n")

    def test_node_without_host(self):
        """Without a host, ports cannot be resolved — the node would silently
        get no runtimes at all."""
        with pytest.raises(ClusterConfigError, match="missing its `host`"):
            parse_cluster("nodes:\n  - id: n\n")

    def test_duplicate_node_id(self):
        """Two entries for one node means one silently wins; which one depends
        on file order, which is not a thing anyone should have to know."""
        with pytest.raises(ClusterConfigError, match="duplicate"):
            parse_cluster("nodes:\n  - id: n\n    host: a\n  - id: n\n    host: b\n")


class TestLoading:
    def test_missing_file_is_not_an_error(self, tmp_path):
        """Means this deployment has not migrated from SPARK_NODES yet, so the
        caller falls back rather than the dashboard going dark."""
        assert load_cluster(tmp_path / "absent.yml") == []

    def test_empty_file(self, tmp_path):
        p = tmp_path / "c.yml"
        p.write_text("")
        assert load_cluster(p) == []

    def test_real_file_round_trip(self, tmp_path):
        p = tmp_path / "c.yml"
        p.write_text(GX10)
        assert by_id(load_cluster(p))["sparky"].runtimes.vllm == ["http://192.168.50.61:8120/metrics"]


class TestAgentConfigEndpoint:
    """What the agent asks for. This is the mechanism that lets the per-node
    stack be identical: the node carries no host-specific runtime config."""

    def _client(self, tmp_path, text):
        from fastapi.testclient import TestClient
        from spark_dash_backend.app import create_app
        from spark_dash_backend.config import Settings

        p = tmp_path / "cluster.yml"
        p.write_text(text)
        return TestClient(
            create_app(
                Settings(
                    spark_nodes="sparky=192.168.50.61",
                    cluster_config=p,
                    prometheus_targets_dir=None,
                )
            )
        )

    def test_serves_a_nodes_runtimes(self, tmp_path):
        with self._client(tmp_path, GX10) as c:
            body = c.get("/api/agent-config", params={"node": "sparky"}).json()
        assert body["configured"] is True
        assert body["runtimes"]["vllm"] == ["http://192.168.50.61:8120/metrics"]
        assert body["runtimes"]["llama_routers"][0] == {
            "url": "http://192.168.50.61:8001",
            "scrape_metrics": True,
        }

    def test_unknown_node_gets_empty_runtimes_not_404(self, tmp_path):
        """A node running but not yet in the cluster file is normal during a
        rollout. Returning an error would have the agent treat it as a failure
        and retry-storm, when the correct behaviour is to poll nothing."""
        with self._client(tmp_path, GX10) as c:
            resp = c.get("/api/agent-config", params={"node": "newcomer"})
        assert resp.status_code == 200
        assert resp.json()["configured"] is False
        assert resp.json()["runtimes"] == {"llama_routers": [], "vllm": []}

    def test_malformed_config_is_an_error_not_an_empty_answer(self, tmp_path):
        """Silently serving "no runtimes" for a typo would leave every node
        reporting no models with nothing explaining why — which reads as an
        outage rather than a mistake in one file."""
        with self._client(tmp_path, "nodes: [unclosed") as c:
            resp = c.get("/api/agent-config", params={"node": "sparky"})
        assert resp.status_code == 500
        assert "cluster config" in resp.json()["detail"]

    def test_missing_file_serves_empty_rather_than_failing(self, tmp_path):
        """Means the deployment has not migrated from SPARK_NODES yet."""
        from fastapi.testclient import TestClient
        from spark_dash_backend.app import create_app
        from spark_dash_backend.config import Settings

        with TestClient(
            create_app(
                Settings(
                    spark_nodes="sparky=192.168.50.61",
                    cluster_config=tmp_path / "absent.yml",
                    prometheus_targets_dir=None,
                )
            )
        ) as c:
            resp = c.get("/api/agent-config", params={"node": "sparky"})
        assert resp.status_code == 200
        assert resp.json()["configured"] is False


class TestClusterNaming:
    """A cluster is NAMED, not sized or numbered.

    `pair` was the old example and it is wrong as soon as a third node joins —
    clusters in the wild run to 32. The field is a free string so any name
    works, including a number for anyone who wants one; what it must never be
    is a magic sentinel, because omitting the key already means standalone.
    """

    def test_a_name_is_carried_through(self):
        nodes = by_id(parse_cluster("""
nodes:
  - id: a
    host: 10.0.0.1
    cluster: inference-west
  - id: b
    host: 10.0.0.2
    cluster: inference-west
  - id: solo
    host: 10.0.0.3
"""))
        assert nodes["a"].cluster == nodes["b"].cluster == "inference-west"
        assert nodes["solo"].cluster is None

    def test_any_scalar_works_including_a_number(self):
        """No schema change is needed to number clusters — but there is no
        sentinel value, so `0` is a cluster called "0" and not standalone."""
        nodes = by_id(parse_cluster("nodes:\n  - id: a\n    host: h\n    cluster: 2\n"))
        assert nodes["a"].cluster == "2"

    def test_absent_means_standalone_not_a_sentinel(self):
        nodes = by_id(parse_cluster("nodes:\n  - id: a\n    host: h\n"))
        assert nodes["a"].cluster is None

    def test_old_group_key_is_still_honoured(self, caplog):
        """Silently ignoring it would drop the node to standalone, and that
        breaks capacity arithmetic in the dangerous direction — free memory
        stops pooling, so a model that would fit reads as one that won't."""
        nodes = by_id(parse_cluster("nodes:\n  - id: a\n    host: h\n    group: legacy\n"))
        assert nodes["a"].cluster == "legacy"

    def test_cluster_wins_when_both_are_present(self):
        nodes = by_id(
            parse_cluster("nodes:\n  - id: a\n    host: h\n    group: old\n    cluster: new\n")
        )
        assert nodes["a"].cluster == "new"


class TestClusterConfigEndpoint:
    """Read-only display of what the dashboard is set up to watch.

    Most of the value people want from a config UI is "what is configured, and
    does it match what I deployed" — which needs no write path, and therefore
    none of the security cost that writing cluster membership through a
    tunnel-published surface would carry (roadmap L3).
    """

    def test_lists_every_configured_node_with_its_runtimes(self, tmp_path, monkeypatch):
        cfg = tmp_path / "cluster.yml"
        cfg.write_text("""
nodes:
  - id: a
    host: 10.0.0.1
    cluster: alpha
    runtimes:
      llama_routers:
        - port: 8001
          scrape_metrics: true
      vllm: [8120]
  - id: b
    host: 10.0.0.2
""")
        from fastapi.testclient import TestClient
        from spark_dash_backend.app import create_app
        from spark_dash_backend.config import Settings

        monkeypatch.setenv("CLUSTER_CONFIG", str(cfg))
        monkeypatch.setenv("SPARK_NODES", "")
        with TestClient(create_app(Settings())) as client:
            body = client.get("/api/cluster/config").json()

        assert [n["node_id"] for n in body["nodes"]] == ["a", "b"]
        a = body["nodes"][0]
        assert a["cluster"] == "alpha"
        assert a["runtimes"]["llama_routers"][0]["scrape_metrics"] is True
        # Ports come back alongside the resolved urls: the UI edits ports, so
        # handing it one saves it parsing a url apart to get there.
        assert a["runtimes"]["vllm"] == [
            {"url": "http://10.0.0.1:8120/metrics", "port": 8120}
        ]
        assert a["runtimes"]["llama_routers"][0]["port"] == 8001
        assert body["nodes"][1]["cluster"] is None

    def test_reports_the_source_so_a_stale_env_is_visible(self, tmp_path, monkeypatch):
        """SPARK_NODES is silently ignored once cluster.yml lists a node, which
        looks identical to an edit that did not take. Naming the source is how
        the UI can say which file is actually in charge."""
        cfg = tmp_path / "cluster.yml"
        cfg.write_text("nodes:\n  - id: a\n    host: 10.0.0.1\n")
        from fastapi.testclient import TestClient
        from spark_dash_backend.app import create_app
        from spark_dash_backend.config import Settings

        monkeypatch.setenv("CLUSTER_CONFIG", str(cfg))
        monkeypatch.setenv("SPARK_NODES", "stale=10.9.9.9")
        with TestClient(create_app(Settings())) as client:
            body = client.get("/api/cluster/config").json()
        assert body["source"] == "cluster.yml"
        assert body["path"].endswith("cluster.yml")
