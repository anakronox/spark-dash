"""The metric contract between the per-node agent and the central backend.

Every section is optional: collectors fail independently, and a node that can't
read (say) PSI should still report GPU and memory rather than dropping out
entirely. `None` means "this collector had nothing to report"; check
`errors` on the snapshot to find out why.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

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


class ProcessInfo(BaseModel):
    """A process holding GPU memory — the nvitop-style process view."""

    pid: int
    name: str
    gpu_mem_bytes: int = Field(ge=0)
    runtime: str | None = Field(
        default=None, description="Inferred runtime, e.g. 'llama.cpp' or 'vllm'."
    )
    model: str | None = None


class LoadedModel(BaseModel):
    """A model currently resident in a llama.cpp router."""

    name: str
    slots_used: int = 0
    slots_total: int = 0
    kv_cache_pct: float | None = None
    tokens_per_sec: float | None = None
    requests_running: int = 0
    requests_waiting: int = 0


class LlamaRouterMetrics(BaseModel):
    """llama.cpp router-mode state.

    Only currently-loaded models appear here. Unloaded models are deliberately
    never probed: GET /metrics?model=X triggers autoload and resets the idle
    sleep timer, so polling them would fight the router's own LRU eviction.
    """

    loaded_models: list[LoadedModel] = Field(default_factory=list)
    known_model_count: int = 0
    tokens_per_sec: float = 0.0


class VllmMetrics(BaseModel):
    """One vLLM instance, scraped from its native /metrics endpoint."""

    model: str
    requests_running: int = 0
    requests_waiting: int = 0
    kv_cache_pct: float | None = None
    tokens_per_sec: float = 0.0
    prompt_tokens_total: int = 0
    generation_tokens_total: int = 0


class Runtimes(BaseModel):
    llama_cpp: LlamaRouterMetrics | None = None
    vllm: list[VllmMetrics] = Field(default_factory=list)


class NodeSnapshot(BaseModel):
    """Everything the dashboard shows for one node at one instant."""

    node_id: str
    ts: datetime
    up: bool = True
    health: HealthState = HealthState.GOOD
    health_reasons: list[str] = Field(
        default_factory=list,
        description="Human-readable causes behind `health`; shown as the label "
        "beside the status color so meaning never rides on hue alone.",
    )

    gpu: GpuMetrics | None = None
    memory: MemoryMetrics | None = None
    psi: PsiMetrics | None = None
    cpu: CpuMetrics | None = None
    processes: list[ProcessInfo] = Field(default_factory=list)
    runtimes: Runtimes = Field(default_factory=Runtimes)

    errors: dict[str, str] = Field(
        default_factory=dict,
        description="collector name -> error message, for collectors that failed.",
    )

    @property
    def total_tokens_per_sec(self) -> float:
        total = sum(v.tokens_per_sec for v in self.runtimes.vllm)
        if self.runtimes.llama_cpp:
            total += self.runtimes.llama_cpp.tokens_per_sec
        return total


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
