"""GPU telemetry via NVML (through nvitop).

Deliberately does *not* report GPU memory. On GB10 the GPU has no private VRAM,
and NVML's memory numbers describe the shared pool misleadingly — see
`memory.py`, which is the single source of truth for memory.

nvitop returns a sentinel `NA` rather than raising when the driver can't supply
a value, so every read goes through `_num()` to turn that into `None`.
"""

from __future__ import annotations

import logging
import time

from nvitop import NA, Device, libnvml
from spark_dash_common.models import GpuMetrics, ProcessInfo

from spark_dash_agent.collectors.base import Collector
from spark_dash_agent.collectors.clock import (
    ClockSignals,
    ClockTracker,
    throttle_threshold_mhz,
)

log = logging.getLogger(__name__)

# An operator-set clock cap (`nvidia-smi -lgc`) is a deliberate action, so it
# reads as LOCKED rather than as a fault.
_LOCKED_REASON_ATTRS = (
    "nvmlClocksEventReasonApplicationsClocksSetting",
    "nvmlClocksEventReasonUserDefinedClocks",
    "nvmlClocksEventReasonDisplayClockSetting",
)

# Hardware or driver pulling clocks down against our wishes — a real fault.
_THROTTLE_REASON_ATTRS = (
    "nvmlClocksEventReasonHwSlowdown",
    "nvmlClocksEventReasonHwThermalSlowdown",
    "nvmlClocksEventReasonHwPowerBrakeSlowdown",
    "nvmlClocksEventReasonSwThermalSlowdown",
)


def _num(value: object) -> float | None:
    """Convert an nvitop reading to a float, mapping its `NA` sentinel to None."""
    if value is NA or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _mask(attr_names: tuple[str, ...]) -> int:
    """Build a bitmask from whichever reason constants this NVML build has."""
    mask = 0
    for name in attr_names:
        mask |= getattr(libnvml, name, 0) or 0
    return mask


def _command_line(proc) -> str:
    """Best-effort command line for a GPU process.

    Matters because several GPU workloads run as a bare `python` process —
    without argv there's no telling them apart. `cmdline()` is tried first
    (structured), then `command()` (shell-escaped string).

    Returns "" when neither is readable: /proc/<pid>/cmdline can be denied when
    the agent runs as non-root and the process belongs to another user, and a
    process can exit mid-scan.
    """
    for attr in ("cmdline", "command"):
        getter = getattr(proc, attr, None)
        if getter is None:
            continue
        try:
            value = getter()
        except Exception:  # noqa: BLE001 — denied or exited; try the next form
            continue
        if not value or value is NA:
            continue
        return " ".join(value) if isinstance(value, list) else str(value)
    return ""


def _cwd(proc) -> str:
    """Working directory, when readable.

    The deciding signal for apps launched as `python main.py`, where neither
    the process name nor argv names the application but the directory does
    (ComfyUI being the case that prompted this).
    """
    getter = getattr(proc, "cwd", None)
    if getter is None:
        return ""
    try:
        value = getter()
    except Exception:  # noqa: BLE001 — denied or exited
        return ""
    return "" if not value or value is NA else str(value)


def infer_runtime(name: str, command: str = "", cwd: str = "") -> str | None:
    """Identify the software behind a GPU process.

    Not every GPU consumer is an LLM runtime — image-generation and notebook
    workloads share the same unified memory pool on GB10, and knowing that is
    the difference between "12GB used, unexplained" and "12GB used by
    ComfyUI". So this labels GPU workloads generally, not just inference
    servers.

    Three signals are needed because process names are frequently useless:
    vLLM and ComfyUI both run as bare `python`, identifiable only by argv and
    working directory respectively.

    Best-effort by design — an unrecognized process still appears in the table,
    just unlabeled. That's honest; a confident wrong guess is not.
    """
    haystack = f"{name} {command} {cwd}".lower()

    # --- LLM inference runtimes ---
    # Atlas is matched on the executable name alone rather than the haystack —
    # see `_looks_like_atlas` — so it is checked first without risk of stealing
    # a process that belongs to another runtime.
    if _looks_like_atlas(name, command):
        return "atlas"
    # vLLM is checked before llama.cpp: a vLLM process serving a Llama model
    # has "llama" in its argv and would otherwise be misattributed.
    if "vllm" in haystack:
        return "vllm"
    if "llama-server" in haystack or "llama.cpp" in haystack or "llama_cpp" in haystack:
        return "llama.cpp"
    if "sglang" in haystack:
        return "sglang"
    if "text-generation" in haystack or "text_generation" in haystack:
        return "tgi"
    if "ollama" in haystack:
        return "ollama"

    # --- other GPU workloads sharing the same memory pool ---
    if "comfy" in haystack or _looks_like_comfyui(haystack):
        return "comfyui"
    if "stable-diffusion" in haystack or "stable_diffusion" in haystack or "sd-webui" in haystack:
        return "stable-diffusion"
    if "jupyter" in haystack or "ipykernel" in haystack:
        return "jupyter"
    return None


# ComfyUI launches as `python main.py` from a directory whose name we usually
# can't read (/proc/<pid>/cwd needs ptrace access the agent doesn't have as
# non-root), so the word "comfy" often appears nowhere. These flags are
# ComfyUI-specific and do appear in argv.
_COMFYUI_FLAGS = (
    "--preview-method",
    "--bf16-unet",
    "--fp16-unet",
    "--bf16-vae",
    "--fp16-vae",
    "--bf16-text-enc",
    "--fp16-text-enc",
    "--enable-manager",
    "--disable-pinned-memory",
    "--use-sage-attention",
    "--disable-smart-memory",
)


#: How far back to ask NVML for process utilization samples.
#
#: The driver keeps only the MOST RECENT sample per process — verified on the
#: GX10, where windows of 1s, 5s and 20s all returned exactly one sample per
#: pid. So this is a staleness bound rather than an averaging window: long
#: enough that a briefly-idle process isn't dropped, short enough that a
#: process which has genuinely stopped working disappears instead of lingering
#: at its last value.
PROCESS_UTIL_WINDOW_S = 5.0


def read_process_utilization(device: Device) -> dict[int, tuple[float, float, float]]:
    """Per-process compute utilization: pid -> (sm, encoder, decoder) percent.

    This is the half of GPU contention that memory can't show. A model can hold
    26GiB and use no SM at all while idle, which looks identical to a busy one
    if you only plot bytes — and on this box the actual compute competition
    turned out to be ComfyUI at 75-91% SM against models that were merely
    resident.

    Processes with no recent activity are ABSENT from the samples rather than
    reported as zero, so a missing pid means idle. Callers should default to 0
    rather than treating it as unknown.

    A caveat worth carrying: these are instantaneous samples of a time-sliced
    resource. They do not sum to overall GPU utilization exactly (measured
    82-96% against an overall 96%), and in principle can exceed it. They answer
    "who is competing" rather than "what fraction of the device".
    """
    getter = getattr(libnvml, "nvmlDeviceGetProcessUtilization", None)
    if getter is None:
        return {}
    since = int((time.time() - PROCESS_UTIL_WINDOW_S) * 1e6)
    try:
        samples = getter(device.handle, since)
    except Exception:  # noqa: BLE001 — unsupported, or simply nothing running
        # NVML raises NotFound rather than returning empty when no process has
        # been active in the window, which is a normal idle state and not worth
        # logging above debug.
        log.debug("process utilization unavailable", exc_info=True)
        return {}

    out: dict[int, tuple[float, float, float]] = {}
    for s in samples:
        pid = getattr(s, "pid", None)
        if pid is None:
            continue
        out[int(pid)] = (
            float(getattr(s, "smUtil", 0) or 0),
            float(getattr(s, "encUtil", 0) or 0),
            float(getattr(s, "decUtil", 0) or 0),
        )
    return out


def _read_applications_clock(device: Device) -> float | None:
    """The SM clock this GPU targets for compute work.

    Distinct from `max_sm_clock`, which is a boost ceiling: on GB10 the ceiling
    reads 3003MHz while the applications clock reads 2418MHz, and three days of
    measurement put the actual clock between 2359 and 2483MHz. The applications
    clock is the number reality tracks.

    It also follows `nvidia-smi -ac`, so an operator who deliberately sets an
    application clock moves the reference with it rather than against it.
    """
    getter = getattr(libnvml, "nvmlDeviceGetApplicationsClock", None)
    if getter is None:
        return None
    # NVML_CLOCK_SM == 1. Named rather than hardcoded where the build exposes it.
    clock_sm = getattr(libnvml, "NVML_CLOCK_SM", 1)
    try:
        value = getter(device.handle, clock_sm)
    except Exception:  # noqa: BLE001 — not supported on every part
        log.debug("applications clock unavailable", exc_info=True)
        return None
    return float(value) if value else None


def infer_model(command: str) -> str | None:
    """The model a process is serving, from `--alias` in its argv.

    llama.cpp's router spawns one child per resident model, and the child is
    where the weights actually live — the router parent holds only its own
    overhead. The child carries `--alias <name>`, and that name is exactly what
    the router reports from `/v1/models`, so it joins to the per-model metrics
    without any fuzzy matching. Verified on the GX10: the 26.4 GiB process
    carried `--alias qwen36-35b`, a model id reported by one router only.

    Returns None for a router parent (`--models-preset`, serving no single
    model) and for anything else without an alias. That's deliberate — an
    unattributed process still shows its memory, it just isn't blamed on a
    model it may not be running.

    vLLM names models differently (`--served-model-name`); adding it here is a
    separate change, not a special case of this one.
    """
    tokens = command.split()
    for i, token in enumerate(tokens):
        if token == "--alias":
            # A trailing `--alias` with no value is malformed; treat as absent
            # rather than reading whatever flag follows it.
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                return tokens[i + 1]
            return None
        if token.startswith("--alias="):
            return token.partition("=")[2] or None
    return None


def _looks_like_atlas(name: str, command: str) -> bool:
    """Identify Atlas, which ships as a single self-contained binary.

    Matched against the executable NAME only — never the argv+cwd haystack the
    other runtimes use. "atlas" is an ordinary English word that turns up in
    repository paths, dataset names and model names, so a bare substring match
    over a full command line is precisely the mislabeling `_looks_like_comfyui`
    exists to avoid.

    argv[0]'s basename is checked alongside the process name because a binary
    invoked by absolute path still reports its own name, but a wrapper (Atlas
    is launched through `sparkrun`) may not.
    """
    candidates = [name.strip().lower()]
    argv0 = command.strip().split(maxsplit=1)
    if argv0:
        candidates.append(argv0[0].rsplit("/", 1)[-1].lower())
    return any(c == "atlas" or c.startswith(("atlas-", "atlas_")) for c in candidates)


def _looks_like_comfyui(haystack: str) -> bool:
    """Identify ComfyUI by its distinctive CLI flags.

    Two matches are required: several of these flags are shared with other
    Stable Diffusion frontends, so any single one is weak evidence. Mislabeling
    is worse than leaving a process unlabeled.
    """
    return sum(flag in haystack for flag in _COMFYUI_FLAGS) >= 2


# Runtimes that serve LLM inference, as opposed to other GPU consumers. Lets
# the UI separate "what's serving models" from "what else is eating the pool".
LLM_RUNTIMES = frozenset({"vllm", "llama.cpp", "sglang", "atlas", "tgi", "ollama"})


class GpuCollector(Collector[GpuMetrics]):
    """Reads the first GPU. GB10 nodes have exactly one."""

    name = "gpu"

    def __init__(self, device_index: int = 0) -> None:
        self._device_index = device_index
        self._tracker = ClockTracker()
        self._locked_mask = _mask(_LOCKED_REASON_ATTRS)
        self._throttle_mask = _mask(_THROTTLE_REASON_ATTRS)
        self._device: Device | None = None
        # Cached so the memory collector can decide UMA-ness without opening
        # its own NVML handle.
        self.memory_total_bytes: int | None = None
        # The silicon's own thermal limits, read once at open. See
        # `_read_temp_thresholds`.
        self.slowdown_temp_c: float | None = None
        self.shutdown_temp_c: float | None = None
        # The clock the GPU targets for compute work — the reference the
        # throttle threshold is derived from. See `_read_applications_clock`.
        self.target_clock_mhz: float | None = None

    def _get_device(self) -> Device:
        if self._device is None:
            self._device = Device(self._device_index)
            self.memory_total_bytes = int(_num(self._device.memory_total()) or 0) or None

            # Calibrate against the clock this GPU actually TARGETS, not its
            # boost ceiling — see `throttle_threshold_mhz`. On GB10 the ceiling
            # (3003MHz) is never approached; the applications clock (2418MHz) is
            # what the observed range brackets.
            self.target_clock_mhz = _read_applications_clock(self._device)
            max_clock = _num(self._device.max_sm_clock())
            threshold = throttle_threshold_mhz(self.target_clock_mhz)
            self._tracker = ClockTracker(throttled_mhz=threshold)
            log.info(
                "clock throttle threshold %.0fMHz (applications clock %s, max_sm_clock %s)",
                threshold,
                self.target_clock_mhz,
                max_clock,
            )
            self._read_temp_thresholds(self._device)
        return self._device

    def _read_temp_thresholds(self, device: Device) -> None:
        """Read the GPU's own thermal limits from NVML.

        Worth doing rather than hardcoding: on GB10 these are 86C (slowdown)
        and 90C (shutdown), which bracket a range that hardcoded guesses got
        wrong from both directions — an 80C "critical" fired during ordinary
        work, while a 94C alert sat above the temperature at which the hardware
        powers itself off and so could never fire at all.

        `nvidia-smi` prints these as N/A on this hardware; they are only
        reachable through NVML. Unsupported on some parts, in which case the
        fallback bands in thresholds.py apply.
        """
        for attr, name in (
            ("slowdown_temp_c", "NVML_TEMPERATURE_THRESHOLD_SLOWDOWN"),
            ("shutdown_temp_c", "NVML_TEMPERATURE_THRESHOLD_SHUTDOWN"),
        ):
            threshold = getattr(libnvml, name, None)
            if threshold is None:
                continue
            try:
                value = libnvml.nvmlDeviceGetTemperatureThreshold(device.handle, threshold)
            except Exception:  # noqa: BLE001 — not supported on every part
                log.debug("%s unavailable", name, exc_info=True)
                continue
            if value:
                setattr(self, attr, float(value))

        log.info(
            "GPU thermal limits: slowdown %s, shutdown %s",
            self.slowdown_temp_c,
            self.shutdown_temp_c,
        )

    def _throttle_signals(self, device: Device) -> tuple[bool | None, bool | None]:
        """Read NVML throttle reasons, if this driver/hardware exposes them.

        GB10 support is uncertain, so a failure here is not an error — it just
        means `classify_clock` falls back to the frequency threshold. Returning
        `None` (rather than False) is what communicates "we don't know".
        """
        getter = getattr(libnvml, "nvmlDeviceGetCurrentClocksEventReasons", None) or getattr(
            libnvml, "nvmlDeviceGetCurrentClocksThrottleReasons", None
        )
        if getter is None:
            return None, None
        try:
            reasons = getter(device.handle)
        except Exception:  # noqa: BLE001 — unsupported on some hardware; fall back
            log.debug("clock throttle reasons unavailable", exc_info=True)
            return None, None
        return bool(reasons & self._locked_mask), bool(reasons & self._throttle_mask)

    def collect(self) -> GpuMetrics:
        device = self._get_device()

        util = _num(device.gpu_utilization()) or 0.0
        power_mw = _num(device.power_draw())
        clock_mhz = _num(device.sm_clock()) or _num(device.graphics_clock())
        locked, hw_throttled = self._throttle_signals(device)

        clock_state = self._tracker.update(
            ClockSignals(
                util_pct=util,
                clock_mhz=clock_mhz,
                locked_by_setting=locked,
                hw_throttled=hw_throttled,
            )
        )

        return GpuMetrics(
            util_pct=min(100.0, max(0.0, util)),
            temp_c=_num(device.temperature()),
            # nvitop reports milliwatts; the dashboard shows watts.
            power_w=power_mw / 1000.0 if power_mw is not None else None,
            clock_mhz=clock_mhz,
            clock_state=clock_state,
            target_clock_mhz=self.target_clock_mhz,
        )

    def collect_processes(self) -> list[ProcessInfo]:
        """Per-process GPU memory — the nvitop-style process view.

        Separate from `collect()` because it's the one GPU reading that can be
        slow (it resolves process names via /proc) and because a failure here
        shouldn't cost us the GPU tiles.
        """
        device = self._get_device()
        out: list[ProcessInfo] = []
        # One NVML call for the whole device rather than one per process.
        utilization = read_process_utilization(device)

        for pid, proc in device.processes().items():
            try:
                name = proc.name()
                gpu_mem = _num(proc.gpu_memory()) or 0.0
                # Read once: it's a /proc access per process, and both the
                # runtime and the model are derived from it.
                command = _command_line(proc)
                # Absent means idle, so 0 is a reading and not a missing value.
                sm, enc, dec = utilization.get(pid, (0.0, 0.0, 0.0))
                out.append(
                    ProcessInfo(
                        pid=pid,
                        name=str(name) if name is not NA else f"pid-{pid}",
                        gpu_mem_bytes=int(gpu_mem),
                        runtime=infer_runtime(str(name), command, _cwd(proc)),
                        model=infer_model(command),
                        sm_pct=sm,
                        encoder_pct=enc,
                        decoder_pct=dec,
                    )
                )
            except Exception:  # noqa: BLE001 — a process exiting mid-scan is normal
                log.debug("skipping process %s", pid, exc_info=True)

        out.sort(key=lambda p: p.gpu_mem_bytes, reverse=True)
        return out
