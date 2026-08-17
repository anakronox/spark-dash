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

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeConfig:
    """What this node should poll.

    Named RuntimeConfig rather than Runtimes to keep it distinct from the
    snapshot's Runtimes, which carries collected METRICS. This is the
    instruction; that is the result.
    """

    llama_routers: list[str] = field(default_factory=list)
    #: Subset of `llama_routers` allowed to serve `/metrics?model=`.
    metrics_allowlist: list[str] = field(default_factory=list)
    vllm: list[str] = field(default_factory=list)


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
    ) -> None:
        self._url = backend_url.rstrip("/")
        self._node_id = node_id
        self._timeout_s = timeout_s
        self._ttl_s = ttl_s

        self._runtimes: RuntimeConfig | None = None
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

    def current(self, now: float) -> RuntimeConfig | None:
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
                    f"{self._url}/api/agent-config", params={"node": self._node_id}
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
        vllm = [str(u) for u in (raw.get("vllm") or []) if u]

        updated = RuntimeConfig(
            llama_routers=routers, metrics_allowlist=allowlist, vllm=vllm
        )
        if updated != self._runtimes:
            log.info(
                "cluster config: %d router(s) (%d scraped for per-model "
                "metrics), %d vLLM endpoint(s)",
                len(routers),
                len(allowlist),
                len(vllm),
            )
        self._runtimes = updated
        self._configured = True
