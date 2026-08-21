"""The metric contract between the per-node agent and the central backend.

Every section is optional: collectors fail independently, and a node that can't
read (say) PSI should still report GPU and memory rather than dropping out
entirely. `None` means "this collector had nothing to report"; check
`errors` on the snapshot to find out why.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ClockState(StrEnum):
    """GPU clock health, evaluated only under load.

    Deliberately load-gated: a low clock at idle is correct behavior, not a
    fault, so `IDLE` means "not evaluated" rather than "healthy".
    """

    IDLE = "IDLE"
    PASS = "PASS"
    LOCKED = "LOCKED"
    THROTTLED = "THROTTLED"


class PsiState(StrEnum):
    """Memory pressure band derived from /proc/pressure/memory."""

    LOW = "LOW"
    MOD = "MOD"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class HealthState(StrEnum):
    """Rolled-up node health, mapped to the reserved status palette.

    Never rendered as color alone — the UI pairs these with an icon and label.
    """

    GOOD = "good"
    WARNING = "warning"
    SERIOUS = "serious"
    CRITICAL = "critical"


class GpuMetrics(BaseModel):
    """GPU telemetry from NVML.

    Note there is no memory field here. On GB10 the GPU has no private VRAM —
    see `MemoryMetrics`, which reports the single coherent pool.
    """

    util_pct: float = Field(ge=0, le=100)
    temp_c: float | None = None
    power_w: float | None = None
    clock_mhz: float | None = None
    clock_state: ClockState = ClockState.IDLE
    target_clock_mhz: float | None = Field(
        default=None,
        description="The SM clock this GPU targets for compute work "
        "(NVML applications clock). Reported so `clock_mhz` can be read as a "
        "fraction of what it should be, rather than as a bare number. NOT the "
        "boost ceiling: on GB10 the ceiling is 3003MHz and never approached, "
        "while the target is 2418MHz and the observed clock brackets it.",
    )


class DiskMetrics(BaseModel):
    """Root filesystem capacity. ROOT ONLY, and deliberately so.

    Model weights live here — 894 GB of them on the first GX10, on a 3.6 TB
    root — so this is the disk that actually fills, and the one whose filling
    stops inference rather than merely losing logs.

    `used_bytes` is `total - available`, NOT `total - free`. The two differ by
    the filesystem's reserved blocks, and `available` is the basis
    `NodeDiskLow` and `NodeDiskCritical` alert on. A card and an alert
    disagreeing about what "full" means is worse than showing neither.

    Never enumerates mounts. `statvfs` on a stale NFS mount blocks forever, and
    these nodes mount a NAS; walking every filesystem would put an unbounded
    hang back into snapshot collection, which is exactly what Q removed.
    """

    total_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)

    @property
    def used_pct(self) -> float:
        """Percent full, on the same basis as the alerts: used / (used + avail)."""
        denom = self.used_bytes + self.available_bytes
        return 100.0 * self.used_bytes / denom if denom else 0.0


class MemoryMetrics(BaseModel):
    """System memory.

    On GB10 this *is* GPU memory: CPU and GPU share one coherent LPDDR5x pool,
    so `nvmlDeviceGetMemoryInfo` reports total-ish regardless of real usage and
    must not be used. `used` is computed as `total - available`, which stays
    accurate under heavy inference load.
    """

    total_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    swap_used_bytes: int = Field(default=0, ge=0)
    unified: bool = Field(
        default=False,
        description="True when CPU and GPU share one coherent pool (GB10).",
    )

    @property
    def used_pct(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return 100.0 * self.used_bytes / self.total_bytes


class PsiMetrics(BaseModel):
    """Pressure Stall Information for memory.

    Catches contention *before* swap or a freeze, which raw percent-used does
    not. `some_*` is "at least one task stalled"; `full_*` is "all tasks
    stalled" and is the more serious signal.
    """

    some_avg10: float = 0.0
    some_avg60: float = 0.0
    full_avg10: float = 0.0
    full_avg60: float = 0.0
    state: PsiState = PsiState.LOW


class CpuMetrics(BaseModel):
    util_pct: float = Field(ge=0, le=100)
    temp_c: float | None = None
    load_avg_1m: float | None = None
    active_cores: int | None = None


class NetworkInterface(BaseModel):
    """One physical network interface.

    Only physical NICs are reported. A node running Docker has dozens of veth
    and bridge interfaces whose counters mean nothing to an operator.
    """

    name: str
    up: bool = True
    monitored: bool = Field(
        default=True,
        description="False when this node's config excludes the interface from "
        "alerting.\n\n"
        "DEFAULT TRUE, and excluded by name rather than selected by name: an "
        "interface nobody has configured is still watched, so forgetting to "
        "maintain the list makes the dashboard noisy rather than silent. It is "
        "reported rather than omitted for the same reason — the panel still "
        "shows the interface, and its history keeps accumulating.\n\n"
        "Distinct from `unmonitored_runtimes`, which is the opposite kind of "
        "fact: that is a gap to be fixed, this is a deliberate exclusion.",
    )
    speed_mbps: int | None = Field(
        default=None,
        description="Negotiated link speed. None when the driver doesn't report "
        "one, which is normal for virtual and some wireless interfaces.",
    )

    rx_bytes_per_sec: float = 0.0
    tx_bytes_per_sec: float = 0.0
    rx_bytes_total: int = 0
    tx_bytes_total: int = 0

    # Errors and drops are cumulative rather than rates: what matters is
    # whether the count is moving at all, not how fast.
    rx_errors: int = 0
    tx_errors: int = 0
    rx_dropped: int = 0
    tx_dropped: int = 0

    @property
    def healthy(self) -> bool:
        return self.up and (self.rx_errors + self.tx_errors) == 0


class RdmaPort(BaseModel):
    """One RDMA port, from /sys/class/infiniband.

    On the GX10s this is a ConnectX-7 running RoCEv2 — RDMA over Ethernet
    rather than native InfiniBand — so `link_layer` reads "Ethernet". The
    device still registers here either way, which is why this reads the
    InfiniBand sysfs tree rather than anything RoCE-specific.
    """

    device: str
    port: int
    monitored: bool = Field(
        default=True,
        description="False when the Ethernet interface this port is paired "
        "with is excluded from alerting.\n\n"
        "DERIVED, never configured separately: one cable carries both, so a "
        "pulled f1 port that is excluded as an interface but not as a RoCE "
        "port would simply trade `NetworkLinkDown` for `RdmaPortDown`. A port "
        "with no netdev pairing stays monitored.",
    )
    state: str = "UNKNOWN"
    physical_state: str = ""
    link_layer: str = ""
    rate: str = Field(
        default="",
        description="Negotiated rate as the driver reports it, e.g. "
        "'200 Gb/sec (2X NDR)'. Worth surfacing: a ConnectX-7 link that "
        "negotiates far below its rated speed is a known and otherwise "
        "invisible failure.",
    )

    interface: str = Field(
        default="",
        description="Ethernet interface this RoCE device is paired with. "
        "Byte counters come from there: mlx5 leaves the InfiniBand-style "
        "counters at zero for an Ethernet link layer.",
    )

    rx_bytes_per_sec: float = 0.0
    tx_bytes_per_sec: float = 0.0
    rx_bytes_total: int = 0
    tx_bytes_total: int = 0
    errors: int = 0

    @property
    def active(self) -> bool:
        return self.state.upper().endswith("ACTIVE")


class ProcessInfo(BaseModel):
    """A process holding GPU memory — the nvitop-style process view."""

    pid: int
    name: str
    gpu_mem_bytes: int = Field(ge=0)
    runtime: str | None = Field(
        default=None, description="Inferred runtime, e.g. 'llama.cpp' or 'vllm'."
    )
    model: str | None = Field(
        default=None,
        description="Model this process is serving, from `--alias` in its argv. "
        "The same string the router reports, so process memory joins to the "
        "per-model router metrics. None for a router parent (which serves no "
        "single model) and for non-LLM workloads.",
    )
    server: str | None = Field(
        default=None,
        description="host:port this model is served from.\n\n"
        "For a llama.cpp child this is the router that owns it, matched by "
        "alias. For vLLM it is the instance's own endpoint, since nothing sits "
        "in front of it. Named `server` rather than `router` because a "
        "standalone llama.cpp, sglang or vLLM process has no router at all, and "
        "a column that only ever populated for one runtime read as missing data "
        "rather than as 'not applicable'.\n\n"
        "None when it genuinely cannot be determined — several routers claiming "
        "the same alias with none ACTIVE, or more than one vLLM instance where "
        "the process cannot be matched to one.",
    )

    # --- compute, as distinct from memory -----------------------------------
    #
    # Memory answers "what is resident"; these answer "what is actually
    # running". They diverge constantly: a model can hold 26GiB and use no SM
    # at all while idle, which looks identical to a busy one if you only plot
    # memory.
    sm_pct: float = Field(
        default=0.0,
        ge=0,
        description="Share of sampled time this process had work on the SMs. "
        "This is the contention that matters for inference latency. Absent "
        "from NVML's samples means idle, so 0 is a reading rather than a gap.",
    )
    encoder_pct: float = Field(
        default=0.0,
        ge=0,
        description="NVENC utilization. A SEPARATE fixed-function block, so a "
        "process at 70% here is not competing for SM — which is exactly why "
        "it is reported apart from sm_pct rather than folded into it.",
    )
    decoder_pct: float = Field(
        default=0.0, ge=0, description="NVDEC utilization. Separate block, as encoder_pct."
    )


class ModelState(StrEnum):
    """Lifecycle of a model registered with a llama.cpp router.

    `SLEEPING` is the state that matters most and the one a boolean
    loaded/not-loaded would hide: the child process is alive but its weights
    have been released after `--sleep-idle-seconds`. It holds only process
    overhead (~200MB), responds instantly to `/v1/models`, and must never be
    sent a `/metrics?model=` request — that reloads it.
    """

    ACTIVE = "active"
    SLEEPING = "sleeping"
    LOADING = "loading"
    UNLOADED = "unloaded"
    UNKNOWN = "unknown"


# Only these states mean weights are resident and metrics are safe to fetch.
# Everything else — including anything unrecognized — is left alone.
SCRAPEABLE_STATES = frozenset({ModelState.ACTIVE})


class RouterModel(BaseModel):
    """One model registered with a router, in whatever state it's in.

    Covers every registered model, not just resident ones: "4 registered, 1
    active, 2 sleeping" is the operationally interesting picture, and a model
    that's merely sleeping is a warm cache rather than a cold start.
    """

    name: str
    state: ModelState = ModelState.UNKNOWN
    raw_status: str = Field(
        default="",
        description="Verbatim status from the router, kept so an unrecognized "
        "value is diagnosable rather than silently collapsed to UNKNOWN.",
    )

    # --- what the model IS, as opposed to what it is doing -------------------
    #
    # From the `meta` block llama.cpp already returns on /v1/models, which was
    # being parsed past. Everything else on this model describes activity;
    # without these a load time is a number you cannot reason about — 15.6 GiB
    # in 90s is ~175 MB/s, which is a disk answer rather than a mystery.
    #
    # All optional: vLLM has no equivalent, and older llama.cpp builds omit
    # `meta` entirely. Absent means unknown, never zero.
    size_bytes: int | None = Field(
        default=None, ge=0, description="Resident footprint of the weights."
    )
    n_params: int | None = Field(default=None, ge=0)
    quantization: str | None = Field(
        default=None, description="llama.cpp `ftype`, e.g. 'Q5_K - Medium'."
    )
    context_length: int | None = Field(
        default=None, ge=0, description="Configured context window, `n_ctx`."
    )

    # Populated only for ACTIVE models — fetching these for any other state
    # would wake the model.
    slots_used: int = 0
    slots_total: int = 0
    kv_cache_pct: float | None = None
    tokens_per_sec: float | None = None
    requests_running: int = 0
    requests_waiting: int = 0


class LlamaRouterMetrics(BaseModel):
    """One llama.cpp router instance.

    A node commonly runs several router containers, so these are reported as a
    list keyed by `endpoint` rather than as a single object.

    Only currently-loaded models appear here. Unloaded models are deliberately
    never probed: GET /metrics?model=X triggers autoload and resets the idle
    sleep timer, so polling them would fight the router's own LRU eviction.
    """

    endpoint: str = Field(description="Base URL, used to tell routers apart.")
    name: str = Field(
        default="",
        description="Friendly label for the UI; falls back to host:port of the endpoint.",
    )
    reachable: bool = True

    models: list[RouterModel] = Field(
        default_factory=list, description="Every registered model, whatever its state."
    )

    # From the router's /props. max_instances is `--models-max`: the ceiling on
    # concurrently resident models, which makes "2 of 3 slots" expressible.
    max_instances: int | None = None
    autoload: bool | None = None

    tokens_per_sec: float = 0.0

    @property
    def active_models(self) -> list[RouterModel]:
        return [m for m in self.models if m.state is ModelState.ACTIVE]

    @property
    def sleeping_models(self) -> list[RouterModel]:
        return [m for m in self.models if m.state is ModelState.SLEEPING]

    @property
    def known_model_count(self) -> int:
        return len(self.models)


class EngineMetrics(BaseModel):
    """One engine instance, scraped from its native Prometheus endpoint.

    Shared by vLLM and SGLang because they answer the same questions in the
    same shape — a text exposition endpoint with running/queued requests and a
    token counter. Only the metric NAMES differ, and those live in the agent's
    `SPECS` table rather than in a second copy of this model.

    It is deliberately not shared with llama.cpp router mode, which is a
    genuinely different thing: one endpoint fronting several models with
    load/unload state, rather than one process serving one model.
    """

    model: str
    reachable: bool = Field(
        default=True,
        description="False when the configured endpoint did not answer.\n\n"
        "An unreachable instance used to be dropped from the list entirely, "
        "which made a typo'd port INVISIBLE: the node reported no instance, "
        "which is indistinguishable from a node that runs none. Silence is the "
        "failure mode this whole area exists to catch, so a configured "
        "endpoint that does not answer is reported as such rather than "
        "omitted. `model` carries the endpoint in that case, since nothing "
        "answered to name itself.",
    )

    server: str = Field(
        default="",
        description="host:port this instance is served from. Nothing fronts a "
        "vLLM or SGLang instance, so this is the endpoint itself — which is "
        "what lets it sit in the same column as a llama.cpp router rather than "
        "showing a gap.",
    )
    requests_running: int = 0
    requests_waiting: int = 0
    kv_cache_pct: float | None = Field(
        default=None,
        description="How full the KV cache is, as a percentage.\n\n"
        "None for engines that do not report it. SGLang is the reason it is "
        "optional rather than defaulted: it publishes `cache_hit_rate`, which "
        "is the fraction of prompt tokens served from the PREFIX cache — a "
        "different question with the same shape. Rendering it here would show "
        "a number that reads as occupancy and is not, so an SGLang row leaves "
        "this empty. An empty cell is honest; a wrong one is not.",
    )
    tokens_per_sec: float = 0.0
    prompt_tokens_total: int = 0
    generation_tokens_total: int = 0


class Runtimes(BaseModel):
    # Lists, not single objects: a node typically runs several llama.cpp router
    # containers alongside several vLLM instances.
    #
    # One field per engine, even though vLLM and SGLang share a model type. The
    # alternative — a single `engines` list with a `runtime` discriminator —
    # would rename the `sparkdash_vllm_*` metric family that alert rules and
    # recorded history are written against, which is a migration this buys
    # nothing. The collector is shared; the wire stays per-engine.
    llama_cpp: list[LlamaRouterMetrics] = Field(default_factory=list)
    vllm: list[EngineMetrics] = Field(default_factory=list)
    sglang: list[EngineMetrics] = Field(default_factory=list)

    @property
    def engines(self) -> dict[str, list[EngineMetrics]]:
        """The `EngineMetrics` fields, keyed by runtime name.

        One place that knows which fields are engines, so the exporter and the
        snapshot builder iterate rather than each naming every engine again.
        Derived from the model's own annotations: a field added above is
        included here without a second edit, and one that is forgotten cannot
        silently fall out.
        """
        return {name: getattr(self, name) for name in ENGINE_RUNTIMES}


#: Engines configured as "a list of /metrics endpoints", in wire order.
#:
#: Derived from `Runtimes` itself so there is one list rather than one per
#: component: the agent builds a collector per entry, the backend parses,
#: renders and retires config per entry, and neither can drift from the
#: snapshot's own shape. llama.cpp is absent on purpose — its config is
#: routers with a per-router opt-in, not a bare list of endpoints.
ENGINE_RUNTIMES: tuple[str, ...] = tuple(
    name
    for name, info in Runtimes.model_fields.items()
    if info.annotation == list[EngineMetrics]
)


class TempBands(BaseModel):
    """The temperature bands in force on a node, and where they came from.

    Reported rather than assumed so alerting can compare against the *node's
    own* thresholds instead of hardcoding numbers in a rule file. That keeps
    one source of truth: previously the agent called 80°C critical while the
    alert rules waited for 88°C, and a third rule sat at 94°C — above the 90°C
    at which a GB10 powers itself off, so it could never have fired.

    `source` distinguishes a hardware-derived band from a fallback guess, which
    is the difference between a threshold you can trust and one you can't.
    """

    gpu_warning_c: float
    gpu_critical_c: float
    gpu_source: str = "fallback"

    cpu_warning_c: float
    cpu_critical_c: float
    cpu_source: str = "fallback"


class ConfigStatus(BaseModel):
    """Where a node's runtime config came from, and when it last arrived.

    Answers "did my edit reach spark3?" without an SSH session — the question
    that made every central-config change a two-step guess.

    The SOURCE matters as much as the timestamp. A node on `env` is not being
    managed centrally at all, which is a different fault from one whose last
    successful fetch is an hour old, and both are different from a node that
    has been asking and never got an answer.
    """

    source: Literal["central", "env", "unreachable"] = Field(
        default="env",
        description="central = cluster.yml on the monitoring VM is in charge. "
        "env = this node is falling back to its own environment variables, "
        "either because it is absent from cluster.yml or because it was never "
        "pointed at a backend. unreachable = it is asking and getting no "
        "answer, so it is running on env by accident rather than by design.",
    )

    fetched_at: datetime | None = Field(
        default=None,
        description="When central last ANSWERED — not when it was last asked. "
        "The agent retries on a TTL whether or not the last attempt worked, so "
        "reporting the attempt would tell a reader their edit had arrived when "
        "the last thing that happened was a timeout.",
    )


class NodeSnapshot(BaseModel):
    """Everything the dashboard shows for one node at one instant."""

    node_id: str
    ts: datetime
    up: bool = True

    agent_version: str = Field(
        default="unknown",
        description="Commit the agent image was built from. A stale agent "
        "presents as a missing feature rather than as a stale agent, so the "
        "build it's running has to be legible from the dashboard.",
    )

    # Which cluster this node belongs to, or None when it stands alone.
    #
    # A NAME, not a size or a number. It is rendered as a heading in the UI and
    # written as a Prometheus label, so it has to read well in an alert at 2am;
    # `cluster="3"` needs a decoder ring that lives nowhere. Naming it after
    # the count ("pair") is worse still — that becomes a lie the moment a node
    # is added, and clusters in the wild run to 32.
    #
    # Set by the backend from its inventory, not by the agent — a node has no
    # way to know what it's been clustered with. Clustering is what makes
    # capacity arithmetic correct: memory sums WITHIN a cluster (a model can
    # span the members' pooled memory) and never across clusters (it can't
    # span machines that aren't clustered).
    cluster: str | None = None
    health: HealthState = HealthState.GOOD
    health_reasons: list[str] = Field(
        default_factory=list,
        description="Human-readable causes behind `health`; shown as the label "
        "beside the status color so meaning never rides on hue alone.",
    )

    unmonitored_runtimes: list[str] = Field(
        default_factory=list,
        description="Inference runtimes running on this node that nothing is "
        "configured to collect from.\n\n"
        "The failure this catches is silence: a vLLM container can run for days "
        "holding GPU memory with no throughput, latency or queue-depth data "
        "reaching the dashboard, and nothing else says so — the node looks "
        "healthy because the parts being measured are healthy.\n\n"
        "Only runtimes there is a collector FOR are reported — llama.cpp, "
        "vLLM and SGLang. Atlas, TGI and ollama are deliberately excluded: "
        "with no collector to configure, flagging them would produce a warning "
        "that can never be resolved, which trains the reader to ignore the "
        "whole indicator. They are still classified as LLM runtimes, so their "
        "memory is attributed to models rather than to `other gpu`.",
    )

    temp_bands: TempBands | None = Field(
        default=None,
        description="Thresholds this node's health was judged against, so a "
        "reader (and an alert rule) can see them rather than guess.",
    )

    gpu: GpuMetrics | None = None
    memory: MemoryMetrics | None = None
    disk: DiskMetrics | None = None
    psi: PsiMetrics | None = None
    cpu: CpuMetrics | None = None
    processes: list[ProcessInfo] = Field(default_factory=list)
    network: list[NetworkInterface] = Field(default_factory=list)
    rdma: list[RdmaPort] = Field(default_factory=list)
    runtimes: Runtimes = Field(default_factory=Runtimes)

    config: ConfigStatus = Field(
        default_factory=ConfigStatus,
        description="Where this node's runtimes came from and when. Lets the "
        "dashboard answer whether a central edit has actually reached the "
        "node, rather than leaving it to be inferred from whether the metrics "
        "changed.",
    )

    errors: dict[str, str] = Field(
        default_factory=dict,
        description="collector name -> error message, for collectors that failed.",
    )

    @property
    def total_tokens_per_sec(self) -> float:
        return (
            sum(v.tokens_per_sec for v in self.runtimes.vllm)
            + sum(s.tokens_per_sec for s in self.runtimes.sglang)
            + sum(r.tokens_per_sec for r in self.runtimes.llama_cpp)
        )


class ClusterSnapshot(BaseModel):
    """What the backend pushes over the WebSocket each tick.

    A full snapshot rather than deltas: a few KB at 1Hz is negligible on a LAN,
    and it keeps both sides stateless (no resync-after-reconnect logic).
    """

    ts: datetime
    nodes: list[NodeSnapshot] = Field(default_factory=list)

    @property
    def nodes_up(self) -> int:
        return sum(1 for n in self.nodes if n.up)

    @property
    def total_tokens_per_sec(self) -> float:
        return sum(n.total_tokens_per_sec for n in self.nodes)
