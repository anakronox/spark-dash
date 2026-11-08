"""llama.cpp router API keys and the agents' shared token: storage, the
routes, and what /api/agent-config hands out (and to whom)."""

from __future__ import annotations

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient
from spark_dash_backend import router_keys
from spark_dash_backend.app import create_app
from spark_dash_backend.cluster import ClusterConfigError, parse_cluster
from spark_dash_backend.config import Settings

GX10 = """
nodes:
  - id: sparky
    host: 192.168.50.61
    runtimes:
      llama_routers:
        - port: 8001
        - port: 8108
  - id: sparketa
    host: 192.168.50.62
    runtimes:
      llama_routers:
        - port: 8108
"""

KEY = "a" * 64
OTHER = "b" * 64


@pytest.fixture
def cluster(tmp_path):
    d = tmp_path / "cluster"
    d.mkdir()
    p = d / "cluster.yml"
    p.write_text(GX10)
    return p


@pytest.fixture
def client(cluster, tmp_path):
    (tmp_path / "targets").mkdir()
    app = create_app(Settings(cluster_config=cluster, prometheus_targets_dir=tmp_path / "targets"))
    with TestClient(app) as c:
        yield c


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


class TestStorage:
    def test_keys_are_owner_only_and_never_in_the_cluster_file(self, cluster):
        router_keys.write_keys(cluster, {"sparky:8108": KEY})
        p = cluster.parent / router_keys.SECRETS_FILE
        assert mode(p) == 0o600
        assert router_keys.read_keys(cluster) == {"sparky:8108": KEY}
        assert KEY not in cluster.read_text()

    def test_a_key_is_one_printable_token(self):
        assert router_keys.check_key(f"  {KEY}\n") == KEY
        for bad in ("", "short", "has space inside it", "x" * 513, "ключ-ключ-ключ"):
            with pytest.raises(ClusterConfigError):
                router_keys.check_key(bad)

    def test_the_token_is_owner_only_and_replaced_wholesale(self, cluster):
        t1 = router_keys.new_token(cluster)
        assert mode(router_keys.token_path(cluster)) == 0o600
        t2 = router_keys.new_token(cluster)
        assert t1 != t2 and router_keys.read_token(cluster) == t2
        router_keys.remove_token(cluster)
        assert router_keys.read_token(cluster) is None

    def test_token_matching(self, cluster):
        assert not router_keys.token_matches(cluster, "Bearer anything")  # none configured
        t = router_keys.new_token(cluster)
        assert router_keys.token_matches(cluster, f"Bearer {t}")
        assert router_keys.token_matches(cluster, f"bearer {t}")
        assert not router_keys.token_matches(cluster, f"Bearer {t}x")
        assert not router_keys.token_matches(cluster, t)
        assert not router_keys.token_matches(cluster, None)
        assert not router_keys.token_matches(cluster, "Bearer ")

    def test_keys_for_maps_by_router_url_and_prune_drops_the_gone(self, cluster):
        nodes = parse_cluster(GX10)
        router_keys.write_keys(cluster, {"sparky:8108": KEY, "sparketa:8108": OTHER, "gone:1": "c" * 9})
        sparky = next(n for n in nodes if n.node_id == "sparky")
        assert router_keys.keys_for(cluster, sparky) == {"http://192.168.50.61:8108": KEY}
        router_keys.prune(cluster, nodes)
        assert set(router_keys.read_keys(cluster)) == {"sparky:8108", "sparketa:8108"}


class TestRoutes:
    def test_set_shows_only_set_and_hint_never_the_key(self, client, cluster):
        r = client.put("/api/router-keys/sparky/8108", json={"key": KEY})
        assert r.status_code == 200
        body = r.json()
        assert body["keys"] == {"sparky:8108": {"set": True, "hint": "…aaaa"}}
        assert KEY not in r.text
        assert KEY not in client.get("/api/router-keys").text
        assert KEY not in client.get("/api/cluster/config").text

    def test_unknown_router_or_bad_key_is_refused(self, client):
        assert client.put("/api/router-keys/sparky/9999", json={"key": KEY}).status_code == 404
        assert client.put("/api/router-keys/nobody/8108", json={"key": KEY}).status_code == 404
        assert client.put("/api/router-keys/sparky/8108", json={"key": "with space x"}).status_code == 400

    def test_clear(self, client, cluster):
        client.put("/api/router-keys/sparky/8108", json={"key": KEY})
        assert client.delete("/api/router-keys/sparky/8108").json()["keys"] == {}
        assert router_keys.read_keys(cluster) == {}

    def test_the_token_is_shown_once(self, client):
        made = client.post("/api/agent-token").json()
        assert made["token"].startswith("sda-") and made["token_set"]
        later = client.get("/api/router-keys").json()
        assert "token" not in later and later["token_set"] and later["token_hint"]
        assert made["token"] not in client.get("/api/router-keys").text
        assert client.delete("/api/agent-token").json()["token_set"] is False


class TestAgentConfig:
    def test_keys_only_with_the_shared_token(self, client):
        client.put("/api/router-keys/sparky/8108", json={"key": KEY})
        token = client.post("/api/agent-token").json()["token"]

        plain = client.get("/api/agent-config", params={"node": "sparky"})
        assert "router_keys" not in plain.json() and KEY not in plain.text

        wrong = client.get("/api/agent-config", params={"node": "sparky"},
                           headers={"Authorization": "Bearer sda-wrong"})
        assert "router_keys" not in wrong.json() and KEY not in wrong.text

        ok = client.get("/api/agent-config", params={"node": "sparky"},
                        headers={"Authorization": f"Bearer {token}"}).json()
        assert ok["router_keys"] == {"http://192.168.50.61:8108": KEY}
        # The rest of the answer is unchanged by the token.
        assert ok["runtimes"] == plain.json()["runtimes"]

    def test_a_node_gets_only_its_own_keys(self, client):
        client.put("/api/router-keys/sparky/8108", json={"key": KEY})
        client.put("/api/router-keys/sparketa/8108", json={"key": OTHER})
        token = client.post("/api/agent-token").json()["token"]
        body = client.get("/api/agent-config", params={"node": "sparketa"},
                          headers={"Authorization": f"Bearer {token}"}).json()
        assert body["router_keys"] == {"http://192.168.50.62:8108": OTHER}

    def test_no_token_configured_means_no_keys_whatever_is_presented(self, client):
        client.put("/api/router-keys/sparky/8108", json={"key": KEY})
        r = client.get("/api/agent-config", params={"node": "sparky"},
                       headers={"Authorization": "Bearer "})
        assert "router_keys" not in r.json()
        r = client.get("/api/agent-config", params={"node": "sparky"},
                       headers={"Authorization": "Bearer sda-anything"})
        assert "router_keys" not in r.json()

    def test_saving_the_cluster_prunes_a_removed_routers_key(self, client, cluster):
        client.put("/api/router-keys/sparky/8108", json={"key": KEY})
        client.put("/api/router-keys/sparky/8001", json={"key": OTHER})
        cfg = client.get("/api/cluster/config").json()
        nodes = cfg["nodes"]
        for n in nodes:
            if n["node_id"] == "sparky":
                n["runtimes"]["llama_routers"] = [r for r in n["runtimes"]["llama_routers"] if r["port"] == 8001]
        payload = {"nodes": [
            {"node_id": n["node_id"], "host": n["host"],
             "llama_routers": [{"port": r["port"]} for r in n["runtimes"]["llama_routers"]]}
            for n in nodes
        ]}
        assert client.put("/api/cluster/config", json=payload).status_code == 200
        assert set(json.loads((cluster.parent / router_keys.SECRETS_FILE).read_text())) == {"sparky:8001"}
