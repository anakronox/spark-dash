"""Runtime configuration fetched from the backend.

WHY THE AGENT ASKS RATHER THAN BEING TOLD. Which routers and vLLM instances a
node serves used to live in that node's own `.env`, which meant the per-node
stack could never be identical — and therefore needed one orchestration repo
per node. Moving it central makes the stack byte-identical everywhere; the node
identifies itself from the host's hostname and asks what it should be polling.

WHY POLLING STAYS ON THE NODE. The obvious alternative — the backend scraping
routers itself — does not work. Deciding whether it is safe to scrape a model's
`/metrics` needs NVML per-process utilization, which only the agent has, and
getting that wrong resets the router's idle timer on every poll and pins the
model in memory indefinitely.

PRECEDENCE: central wins WHERE CENTRAL HAS AN OPINION.

  node is in cluster.yml        -> central config, env ignored
  node is absent from it        -> fall back to env
  backend unreachable           -> last known config, else env

That middle case is what makes a rollout safe. Deploying this agent before
adding the node to `cluster.yml` would otherwise take the node's model
reporting dark the moment it restarts, which is a bad way to find out about an
ordering mistake.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx
from spark_dash_common.sealed import SealError, open_sealed

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NodeConfig:
    """What this node should poll, and what it should stay quiet about.

    Named for the node rather than for runtimes because it is no longer only
    runtimes: the interface ignore list rides the same fetch. Kept distinct
    from the snapshot's `Runtimes`, which carries collected METRICS — this is
    the instruction, that is the result.
    """

    llama_routers: list[str] = field(default_factory=list)
    #: Subset of `llama_routers` allowed to serve `/metrics?model=`.
    metrics_allowlist: list[str] = field(default_factory=list)
    #: runtime name -> /metrics endpoints, for the engines that share one
    #: collector (vLLM, SGLang). A mapping rather than a field per engine so a
    #: new engine is a new key in the cluster file and nothing here.
    engines: dict[str, list[str]] = field(default_factory=dict)
    #: Interfaces excluded from alerting, by name. Empty means watch
    #: everything, which is also what an unconfigured node gets.
    ignored_interfaces: list[str] = field(default_factory=list)
    #: Runtimes a PEER in this node's cluster is already collecting from.
    #: A distributed model is served by the cluster, so a worker holding
    #: weights with no API of its own is accounted for even though nothing on
    #: this node polls it. Empty for a standalone node.
    cluster_collected_runtimes: list[str] = field(default_factory=list)
    #: RoCE addresses to probe for round-trip time (CS). Empty means no
    #: probe at all on this node.
    rdma_peers: list[str] = field(default_factory=list)
    #: URL -> its API key, for llama.cpp routers and engines started with
    #: one: a router's URL, an engine's /metrics URL. Named for the routers,
    #: which had keys first. Only sent by the backend to an agent presenting
    #: the shared token. Kept out of repr so no log line can carry a key; still
    #: compared, so a key set or cleared in Settings rebuilds the collectors.
    router_keys: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def vllm(self) -> list[str]:
        """Kept because `vllm` is the engine every deployment already has."""
        return self.engines.get("vllm", [])


class RemoteConfig:
    """Fetches this node's runtimes from the backend, with a TTL.

    Keeps the last successful answer in memory so a backend blip does not
    momentarily blank a node's routers — a transient fetch failure should not
    look like "this node serves nothing".

    NOT cached to disk, deliberately. Surviving an agent restart during a
    backend outage would need a writable volume on every node, and the node
    stack has no persistent state at all today — that property is worth more
    than closing a narrow window. During a backend outage the dashboard is down
    anyway; what is lost is model metrics in Prometheus for nodes that also
    happen to restart in that window.
    """

    def __init__(
        self,
        backend_url: str,
        node_id: str,
        *,
        timeout_s: float = 5.0,
        ttl_s: float = 60.0,
        token: str = "",
        sealed: bool = False,
    ) -> None:
        self._url = backend_url.rstrip("/")
        #: The agents' shared token (AGENT_TOKEN). Never logged.
        self._token = token.strip()
        #: AGENT_SEALED_KEYS: ask for the keys sealed with the token, and
        #: never send the token itself.
        self._sealed = sealed and bool(self._token)
        self._logged_seal_error = False
        self._node_id = node_id
        self._timeout_s = timeout_s
        self._ttl_s = ttl_s

        self._runtimes: NodeConfig | None = None
        #: True once the backend has confirmed this node IS in cluster.yml.
        #: Distinct from having runtimes: a configured node may legitimately
        #: serve nothing, and that must override env rather than fall back.
        self._configured = False
        #: When the next refresh is due. Advanced on FAILURE too, deliberately,
        #: so a dead backend is retried on the TTL rather than on every tick.
        self._fetched_at = 0.0
        #: When central last actually ANSWERED, or None. Separate from the
        #: field above because that one moves on failure — reporting it as the
        #: fetch time would tell a reader their edit had arrived when the last
        #: thing that happened was a timeout. This is what F6 surfaces.
        self._last_ok: float | None = None
        self._logged_absent = False

    @property
    def enabled(self) -> bool:
        return bool(self._url and self._node_id)

    def status(self, now: float) -> tuple[str, float | None]:
        """Where this node's runtimes came from, and when central last answered.

        Answers "did my edit reach spark3?" without an SSH session. The source
        matters as much as the timestamp: a node falling back to `env` is not
        being managed centrally at all, which is a different problem from one
        whose last fetch is stale.
        """
        if not self.enabled:
            return "env", None
        if self._last_ok is None:
            # Asked and never answered — still env, but for a reason worth
            # distinguishing from "never configured to ask".
            return "unreachable", None
        return ("central" if self._configured else "env"), self._last_ok

    def current(self, now: float) -> NodeConfig | None:
        """This node's central runtimes, or None if central has no opinion.

        None means "fall back to env" — either the backend has never answered,
        or it answered that this node is not in the cluster file.
        """
        if not self.enabled:
            return None
        if now - self._fetched_at >= self._ttl_s:
            self._refresh(now)
        return self._runtimes if self._configured else None

    def _refresh(self, now: float) -> None:
        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                resp = client.get(
                    f"{self._url}/api/agent-config",
                    params={"node": self._node_id, **({"sealed": "1"} if self._sealed else {})},
                    headers=(
                        {"Authorization": f"Bearer {self._token}"}
                        if self._token and not self._sealed
                        else {}
                    ),
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception:  # noqa: BLE001 — keep the last good answer
            # Debug, not warning: the backend restarting is routine, and this
            # runs every TTL. A warning here would bury the log in noise for a
            # condition that resolves itself.
            log.debug("agent config fetch failed; keeping last known", exc_info=True)
            self._fetched_at = now
            return

        self._fetched_at = now
        self._last_ok = now
        configured = bool(payload.get("configured"))

        if not configured:
            if not self._logged_absent:
                log.warning(
                    "node %r is not in the cluster config; falling back to "
                    "environment variables. Add it to cluster.yml on the "
                    "monitoring VM to manage it centrally.",
                    self._node_id,
                )
                self._logged_absent = True
            self._configured = False
            self._runtimes = None
            return

        self._logged_absent = False
        raw = payload.get("runtimes") or {}
        routers = [
            str(r.get("url", "")).rstrip("/")
            for r in (raw.get("llama_routers") or [])
            if isinstance(r, dict) and r.get("url")
        ]
        allowlist = [
            str(r.get("url", "")).rstrip("/")
            for r in (raw.get("llama_routers") or [])
            if isinstance(r, dict) and r.get("url") and r.get("scrape_metrics")
        ]
        # Every key the backend sends that is not the routers is an engine
        # list. Read that way rather than from a hardcoded set of names so a
        # node running a newer backend picks up a new engine without needing a
        # newer agent — the agent still only COLLECTS from engines it has a
        # spec for, so an unknown key costs nothing.
        engines = {
            str(key): [str(u) for u in (value or []) if u]
            for key, value in raw.items()
            if key != "llama_routers" and isinstance(value, list)
        }
        engines = {k: v for k, v in engines.items() if v}

        # A SIBLING of `runtimes`, deliberately — see the engine loop above,
        # which claims every list-valued key under `runtimes` for an engine.
        # Nested, this list would be read as an engine named "interfaces".
        raw_interfaces = payload.get("interfaces")
        ignored = []
        if isinstance(raw_interfaces, dict):
            ignored = [
                str(i).strip()
                for i in (raw_interfaces.get("ignore") or [])
                if str(i).strip()
            ]

        cluster_collected = [
            str(r).strip()
            for r in (payload.get("cluster_collected_runtimes") or [])
            if str(r).strip()
        ]

        raw_probe = payload.get("rdma_probe")
        rdma_peers = (
            [str(p).strip() for p in (raw_probe.get("peers") or []) if str(p).strip()]
            if isinstance(raw_probe, dict)
            else []
        )

        # Keys for the routers and engines this node polls, and nothing else.
        # Absent unless the backend accepted the shared token.
        raw_keys = payload.get("router_keys")
        if self._sealed:
            raw_keys = None
            if "sealed_keys" in payload:
                try:
                    raw_keys = open_sealed(self._token, self._node_id, payload["sealed_keys"])
                    self._logged_seal_error = False
                except SealError as exc:
                    if not self._logged_seal_error:
                        log.warning(
                            "%s: is AGENT_TOKEN the token made in Settings? "
                            "Polling without keys.", exc,
                        )
                        self._logged_seal_error = True
        keys = {}
        if isinstance(raw_keys, dict):
            wanted = set(routers) | {u.rstrip("/") for v in engines.values() for u in v}
            keys = {
                str(u).rstrip("/"): str(k)
                for u, k in raw_keys.items()
                if str(u).rstrip("/") in wanted and isinstance(k, str) and k
            }

        updated = NodeConfig(
            llama_routers=routers,
            metrics_allowlist=allowlist,
            engines=engines,
            ignored_interfaces=ignored,
            cluster_collected_runtimes=cluster_collected,
            rdma_peers=rdma_peers,
            router_keys=keys,
        )
        if updated != self._runtimes:
            log.info(
                "cluster config: %d router(s) (%d scraped for per-model "
                "metrics), %s",
                len(routers),
                len(allowlist),
                ", ".join(f"{len(v)} {k} endpoint(s)" for k, v in sorted(engines.items()))
                or "no engine endpoints",
            )
            if ignored:
                log.info(
                    "interfaces excluded from alerting: %s", ", ".join(ignored)
                )
            if keys:
                # Which routers, never the keys.
                log.info(
                    "API keys%s for: %s",
                    " (sealed)" if self._sealed else "",
                    ", ".join(sorted(keys)),
                )
            if cluster_collected:
                log.info(
                    "collected by a cluster peer, so not flagged here: %s",
                    ", ".join(cluster_collected),
                )
        self._runtimes = updated
        self._configured = True
