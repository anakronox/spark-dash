"""The cluster definition parser.

The point of this file is that a node's runtimes stop living in that node's own
`.env`, which is what let the per-node stack become identical everywhere. So
the tests care most about two things: that ports resolve against the right
host, and that a malformed file fails loudly rather than degrading to "no
runtimes" — which would leave every node reporting no models with nothing
saying why.
"""

import pytest
import yaml
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
        assert resp.json()["runtimes"] == {
            "llama_routers": [],
            "vllm": [],
            "sglang": [],
        }

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


class TestCopyYamlRoundTrip:
    """The block the dashboard offers for pasting must actually load.

    F9's generator emits YAML by hand in the frontend, where nothing type-checks
    it against the loader. Its first version put `llama_routers:` and `vllm:` at
    node level instead of under `runtimes:`, and used `- port: N` for vLLM where
    the schema wants a bare number. That parses as valid YAML and loads as a
    node with NO runtimes — so pasting it would look like it worked and then
    silently collect nothing, which is the exact failure this area exists to
    prevent.

    This pins the shape the generator has to produce. If the schema moves, this
    fails and the generator gets fixed with it.
    """

    def test_generated_block_loads_with_its_runtimes_intact(self, tmp_path):
        # Byte-for-byte what Settings.svelte's yamlFor() emits.
        generated = "\n".join(
            [
                "- id: spark2",
                "  host: 192.168.50.62",
                "  cluster: alpha",
                "  agent_port: 9501",
                "  runtimes:",
                "    llama_routers:",
                "      - port: 8001",
                "        scrape_metrics: true",
                "      - port: 8108",
                "    vllm:",
                "      - 8120",
            ]
        )
        path = tmp_path / "cluster.yml"
        path.write_text(f"nodes:\n{generated}\n")

        nodes = load_cluster(path)
        assert len(nodes) == 1
        node = nodes[0]
        assert node.node_id == "spark2"
        assert node.cluster == "alpha"
        assert node.agent_port == 9501
        # The point of the test: the runtimes survived the round trip.
        # RouterConfig holds the RESOLVED url, so the ports are asserted
        # through it rather than as a field.
        assert [r.url for r in node.runtimes.llama_routers] == [
            "http://192.168.50.62:8001",
            "http://192.168.50.62:8108",
        ]
        assert [r.scrape_metrics for r in node.runtimes.llama_routers] == [True, False]
        assert len(node.runtimes.vllm) == 1
        assert "8120" in node.runtimes.vllm[0]

    def test_the_shape_that_used_to_be_generated_loads_with_nothing(self, tmp_path):
        """Proof the old output was silently wrong rather than an error."""
        path = tmp_path / "cluster.yml"
        path.write_text(
            "nodes:\n"
            "- id: spark2\n"
            "  host: 192.168.50.62\n"
            "  llama_routers:\n"
            "    - port: 8001\n"
            "  vllm:\n"
            "    - port: 8120\n"
        )
        node = load_cluster(path)[0]
        assert node.runtimes.llama_routers == []
        assert node.runtimes.vllm == []


class TestInterfaceExclusions:
    """Which interfaces alerting watches, round-tripped through the file.

    The pain this fixes: two 200Gb ports per node were cabled to a switch as a
    test and unplugged, and having been up they read as failed links forever —
    eight firing series across two nodes, re-notified daily because a
    dashboard silence is capped at 24h on purpose.
    """

    def test_ignore_list_is_parsed(self, tmp_path):
        path = tmp_path / "cluster.yml"
        path.write_text(
            "nodes:\n- id: sparketa\n  host: 192.168.50.62\n"
            "  interfaces:\n    ignore:\n      - enP2p1s0f1np1\n      - enp1s0f1np1\n"
        )
        node = load_cluster(path)[0]
        assert node.interfaces.ignore == ["enP2p1s0f1np1", "enp1s0f1np1"]

    def test_a_node_with_no_block_watches_everything(self, tmp_path):
        """Excluded by name, never selected by name. Forgetting the block is
        noisy rather than silent, which is the safe direction here."""
        path = tmp_path / "cluster.yml"
        path.write_text("nodes:\n- id: sparky\n  host: 192.168.50.61\n")
        assert load_cluster(path)[0].interfaces.ignore == []

    def test_a_bare_list_is_read_as_the_ignore_list(self, tmp_path):
        """What someone writes from memory, and the only meaning it could
        have."""
        path = tmp_path / "cluster.yml"
        path.write_text(
            "nodes:\n- id: sparky\n  host: 192.168.50.61\n"
            "  interfaces:\n    - wlP9s9\n"
        )
        assert load_cluster(path)[0].interfaces.ignore == ["wlP9s9"]

    def test_a_malformed_block_does_not_disarm_alerting(self, tmp_path):
        """The failure direction matters: reading garbage as "ignore
        everything" would silently stop every link alert on the node."""
        path = tmp_path / "cluster.yml"
        path.write_text(
            "nodes:\n- id: sparky\n  host: 192.168.50.61\n  interfaces: 7\n"
        )
        assert load_cluster(path)[0].interfaces.ignore == []

    def test_round_trips_through_a_write(self, tmp_path):
        """The dashboard rewrites this file whenever the cluster is edited, so
        an exclusion that did not survive a write would come back as alerts the
        next time someone changed a port."""
        from spark_dash_backend.cluster import write_cluster

        path = tmp_path / "cluster.yml"
        path.write_text(
            "nodes:\n- id: sparketa\n  host: 192.168.50.62\n"
            "  interfaces:\n    ignore:\n      - enP2p1s0f1np1\n"
        )
        write_cluster(path, load_cluster(path))
        assert load_cluster(path)[0].interfaces.ignore == ["enP2p1s0f1np1"]

    def test_the_agent_is_served_the_list_beside_runtimes_not_inside_it(self, tmp_path):
        """LOAD-BEARING PLACEMENT. The agent reads every list-valued key under
        `runtimes` as an engine's endpoints, so that it can pick up an engine a
        newer backend knows about. Nested there, an ignore list would parse as
        an engine named "interfaces" — scraped by nothing, and silently wrong.
        """
        from fastapi.testclient import TestClient
        from spark_dash_backend.app import create_app
        from spark_dash_backend.config import Settings

        path = tmp_path / "cluster.yml"
        path.write_text(
            "nodes:\n- id: sparketa\n  host: 192.168.50.62\n"
            "  runtimes:\n    vllm:\n      - 8120\n"
            "  interfaces:\n    ignore:\n      - enP2p1s0f1np1\n"
        )
        app = create_app(
            Settings(
                spark_nodes="sparketa=192.168.50.62",
                cluster_config=path,
                prometheus_targets_dir=None,
            )
        )
        with TestClient(app) as c:
            body = c.get("/api/agent-config", params={"node": "sparketa"}).json()

        assert body["interfaces"] == {"ignore": ["enP2p1s0f1np1"]}
        assert "interfaces" not in body["runtimes"]


class TestInterfaceExclusionsSurviveAWrite:
    def _client(self, tmp_path, text):
        from fastapi.testclient import TestClient
        from spark_dash_backend.app import create_app
        from spark_dash_backend.config import Settings

        path = tmp_path / "cluster.yml"
        path.write_text(text)
        return (
            TestClient(
                create_app(
                    Settings(
                        spark_nodes="sparketa=192.168.50.62",
                        cluster_config=path,
                        prometheus_targets_dir=None,
                    )
                )
            ),
            path,
        )

    def test_a_name_the_editor_cannot_see_is_still_written(self, tmp_path):
        """The editor sends back names it may not currently observe — a NIC
        absent because the node is down, or renamed. Dropping them on write
        would silently re-arm an alert someone deliberately turned off."""
        client, path = self._client(
            tmp_path, "nodes:\n- id: sparketa\n  host: 192.168.50.62\n"
        )
        with client as c:
            resp = c.put(
                "/api/cluster/config",
                json={
                    "nodes": [
                        {
                            "node_id": "sparketa",
                            "host": "192.168.50.62",
                            "ignored_interfaces": [
                                "enP2p1s0f1np1",
                                "a-nic-nobody-can-see",
                            ],
                        }
                    ]
                },
            )
        assert resp.status_code == 200, resp.text
        assert load_cluster(path)[0].interfaces.ignore == [
            "enP2p1s0f1np1",
            "a-nic-nobody-can-see",
        ]
        assert resp.json()["nodes"][0]["ignored_interfaces"] == [
            "enP2p1s0f1np1",
            "a-nic-nobody-can-see",
        ]

    def test_an_empty_list_removes_the_block_entirely(self, tmp_path):
        """Un-ticking the last box has to actually re-arm the alert, not leave
        an empty `interfaces:` key that reads as configuration."""
        client, path = self._client(
            tmp_path,
            "nodes:\n- id: sparketa\n  host: 192.168.50.62\n"
            "  interfaces:\n    ignore:\n      - enP2p1s0f1np1\n",
        )
        with client as c:
            resp = c.put(
                "/api/cluster/config",
                json={
                    "nodes": [
                        {
                            "node_id": "sparketa",
                            "host": "192.168.50.62",
                            "ignored_interfaces": [],
                        }
                    ]
                },
            )
        assert resp.status_code == 200, resp.text
        assert load_cluster(path)[0].interfaces.ignore == []
        assert "interfaces" not in path.read_text()


class TestRetiringAnEngineEndpoint:
    """The retire button, against a real cluster file.

    Two engines on one node can answer on addresses that differ by port alone,
    so retiring has to touch exactly the engine named by the job — removing
    across all of them would take out an endpoint nobody asked about.
    """

    TWO_ENGINES = (
        "nodes:\n- id: sparky\n  host: 192.168.50.61\n"
        "  runtimes:\n"
        "    vllm:\n      - 8120\n      - 8121\n"
        "    sglang:\n      - 30000\n"
    )

    def _client(self, tmp_path):
        from fastapi.testclient import TestClient
        from spark_dash_backend.app import create_app
        from spark_dash_backend.config import Settings

        path = tmp_path / "cluster.yml"
        path.write_text(self.TWO_ENGINES)
        client = TestClient(
            create_app(
                Settings(
                    spark_nodes="sparky=192.168.50.61",
                    cluster_config=path,
                    prometheus_targets_dir=None,
                )
            )
        )
        return client, path

    def test_retiring_one_engines_endpoint_leaves_the_other_alone(self, tmp_path):
        client, path = self._client(tmp_path)
        with client as c:
            resp = c.delete(
                "/api/targets/absent",
                params={"job": "sglang", "instance": "192.168.50.61:30000"},
            )
        assert resp.status_code == 200

        node = load_cluster(path)[0]
        assert node.runtimes.engines.get("sglang") in (None, [])
        assert node.runtimes.vllm == [
            "http://192.168.50.61:8120/metrics",
            "http://192.168.50.61:8121/metrics",
        ]

    def test_a_port_that_matches_another_engine_is_not_retired(self, tmp_path):
        """Named by job, matched by authority. Asking to retire vLLM's 8120
        must not reach an SGLang entry that happens to share a port on another
        node — and must not silently succeed against the wrong engine."""
        client, path = self._client(tmp_path)
        with client as c:
            resp = c.delete(
                "/api/targets/absent",
                params={"job": "sglang", "instance": "192.168.50.61:8120"},
            )
        assert resp.status_code == 404
        assert len(load_cluster(path)[0].runtimes.vllm) == 2


class TestVllmScrapeTargetsAreGenerated:
    """Retiring an endpoint must stop Prometheus scraping it.

    The first version of G4 removed the endpoint from cluster.yml only, which
    is the AGENT's polling config. Prometheus read a separate hand-maintained
    file, so it carried on scraping, `up == 0` persisted, and the "configured
    but absent" banner correctly came back — making the button look broken.
    Two sources for one fact. These pin the single source.
    """

    def test_every_configured_endpoint_becomes_a_scrape_target(self, tmp_path):
        from spark_dash_backend.inventory import render_engine_file_sd

        path = tmp_path / "cluster.yml"
        path.write_text(
            "nodes:\n"
            "- id: sparky\n"
            "  host: 192.168.50.61\n"
            "  cluster: alpha\n"
            "  runtimes:\n"
            "    vllm:\n"
            "      - 8120\n"
            "      - 8121\n"
        )
        out = yaml.safe_load(render_engine_file_sd(load_cluster(path), "vllm", header="# x"))
        # ONE ENTRY PER ENDPOINT, not per node — a node may serve several.
        assert [e["targets"] for e in out] == [
            ["192.168.50.61:8120"],
            ["192.168.50.61:8121"],
        ]
        # host:port, never the scrape URL: the config holds a path, Prometheus
        # wants an authority.
        assert all("/" not in t for e in out for t in e["targets"])
        assert out[0]["labels"] == {"node": "sparky", "cluster": "alpha"}

    def test_removing_it_from_the_cluster_file_removes_the_target(self, tmp_path):
        """The whole point: retire has to reach Prometheus, not just the agent."""
        from spark_dash_backend.inventory import render_engine_file_sd

        path = tmp_path / "cluster.yml"
        path.write_text(
            "nodes:\n- id: sparky\n  host: 192.168.50.61\n"
            "  runtimes:\n    vllm:\n      - 8120\n"
        )
        assert yaml.safe_load(render_engine_file_sd(load_cluster(path), "vllm", header="# x"))

        # ...as the retire endpoint leaves it.
        path.write_text("nodes:\n- id: sparky\n  host: 192.168.50.61\n")
        # An empty LIST, not a missing file: Prometheus reads it and ends up
        # with no vLLM targets, which is what retiring the last one means.
        assert yaml.safe_load(render_engine_file_sd(load_cluster(path), "vllm", header="# x")) == []

    def test_each_engine_gets_its_own_target_file(self, tmp_path):
        """One file per engine, matching one scrape job per engine. Pooling
        them would put targets that publish differently-named series behind a
        single `job` label."""
        from spark_dash_backend.inventory import write_prometheus_targets

        path = tmp_path / "cluster.yml"
        path.write_text(
            "nodes:\n- id: sparky\n  host: 192.168.50.61\n"
            "  runtimes:\n    vllm:\n      - 8120\n    sglang:\n      - 30000\n"
        )
        targets = tmp_path / "targets"
        targets.mkdir()
        write_prometheus_targets([], targets, cluster_nodes=load_cluster(path))

        assert "192.168.50.61:8120" in (targets / "vllm.yml").read_text()
        assert "192.168.50.61:30000" in (targets / "sglang.yml").read_text()
        # Not in each other's file: the job label is how a rule tells them
        # apart, and the metric names behind it are not interchangeable.
        assert "30000" not in (targets / "vllm.yml").read_text()

    def test_env_fallback_does_not_render_an_empty_vllm_file(self, tmp_path):
        """Under SPARK_NODES there are no runtimes to render. Writing an empty
        list would silently retire every vLLM target the moment someone fell
        back to env."""
        from spark_dash_backend.inventory import write_prometheus_targets

        targets = tmp_path / "targets"
        targets.mkdir()
        write_prometheus_targets([], targets, source="env", cluster_nodes=None)
        assert not (targets / "vllm.yml").exists()
        assert not (targets / "sglang.yml").exists()


def test_saving_the_cluster_rewrites_prometheus_targets(tmp_path):
    """Invalidating the cache is not enough — the target files are what
    Prometheus reads.

    `sync_prometheus_targets` was only ever called at startup, so a node added
    from settings got no scrape target until the backend happened to restart,
    and a retired endpoint kept being scraped. Both looked like the write had
    silently failed.
    """
    from spark_dash_backend.inventory import Inventory

    cfg = tmp_path / "cluster.yml"
    cfg.write_text(
        "nodes:\n- id: sparky\n  host: 192.168.50.61\n"
        "  runtimes:\n    vllm:\n      - 8120\n"
    )
    targets = tmp_path / "targets"
    targets.mkdir()

    inv = Inventory(cluster_config=cfg, prometheus_targets_dir=targets)
    inv.sync_prometheus_targets()
    assert "192.168.50.61:8120" in (targets / "vllm.yml").read_text()

    # The endpoint is retired from the file...
    cfg.write_text("nodes:\n- id: sparky\n  host: 192.168.50.61\n")
    inv.invalidate()
    inv.sync_prometheus_targets()
    # ...and stops being a scrape target.
    assert "8120" not in (targets / "vllm.yml").read_text()


def test_a_target_file_it_cannot_write_is_reported_not_just_logged(tmp_path):
    """This failed for an entire deploy on a leftover root-owned file the
    backend's uid could not overwrite, and the only trace was one WARNING.

    The consequence is not cosmetic: Prometheus keeps whatever targets it had,
    so a node added is never scraped and a retired one is scraped forever. A
    dashboard whose whole purpose is surfacing config that is quietly wrong
    must not hide its own.
    """
    import os

    from spark_dash_backend.inventory import (
        TARGET_WRITE_FAILURES,
        Node,
        write_prometheus_targets,
    )

    targets = tmp_path / "targets"
    targets.mkdir()
    blocked = targets / "agents.yml"
    blocked.write_text("# pre-existing\n")
    blocked.chmod(0o444)

    nodes = [Node(node_id="sparky", host="192.168.50.61")]
    try:
        write_prometheus_targets(nodes, targets, source="cluster.yml")
    finally:
        blocked.chmod(0o644)

    if os.geteuid() != 0:  # root ignores the mode bits
        assert any("agents.yml" in f for f in TARGET_WRITE_FAILURES)
