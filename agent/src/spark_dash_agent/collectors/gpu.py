"""GPU telemetry via NVML (through nvitop).

Deliberately does *not* report GPU memory. On GB10 the GPU has no private VRAM,
and NVML's memory numbers describe the shared pool misleadingly — see
`memory.py`, which is the single source of truth for memory.

nvitop returns a sentinel `NA` rather than raising when the driver can't supply
a value, so every read goes through `_num()` to turn that into `None`.
"""

from __future__ import annotations

import logging

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


def _looks_like_comfyui(haystack: str) -> bool:
    """Identify ComfyUI by its distinctive CLI flags.

    Two matches are required: several of these flags are shared with other
    Stable Diffusion frontends, so any single one is weak evidence. Mislabeling
    is worse than leaving a process unlabeled.
    """
    return sum(flag in haystack for flag in _COMFYUI_FLAGS) >= 2


# Runtimes that serve LLM inference, as opposed to other GPU consumers. Lets
# the UI separate "what's serving models" from "what else is eating the pool".
LLM_RUNTIMES = frozenset({"vllm", "llama.cpp", "sglang", "tgi", "ollama"})


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

    def _get_device(self) -> Device:
        if self._device is None:
            self._device = Device(self._device_index)
            self.memory_total_bytes = int(_num(self._device.memory_total()) or 0) or None

            # Calibrate the throttle threshold against this GPU's own maximum
            # rather than a value hardcoded for one board.
            max_clock = _num(self._device.max_sm_clock())
            threshold = throttle_threshold_mhz(max_clock)
            self._tracker = ClockTracker(throttled_mhz=threshold)
            log.info(
                "clock throttle threshold %.0fMHz (max_sm_clock %s)",
                threshold,
                max_clock,
            )
        return self._device

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
        )

    def collect_processes(self) -> list[ProcessInfo]:
        """Per-process GPU memory — the nvitop-style process view.

        Separate from `collect()` because it's the one GPU reading that can be
        slow (it resolves process names via /proc) and because a failure here
        shouldn't cost us the GPU tiles.
        """
        device = self._get_device()
        out: list[ProcessInfo] = []

        for pid, proc in device.processes().items():
            try:
                name = proc.name()
                gpu_mem = _num(proc.gpu_memory()) or 0.0
                # Read once: it's a /proc access per process, and both the
                # runtime and the model are derived from it.
                command = _command_line(proc)
                out.append(
                    ProcessInfo(
                        pid=pid,
                        name=str(name) if name is not NA else f"pid-{pid}",
                        gpu_mem_bytes=int(gpu_mem),
                        runtime=infer_runtime(str(name), command, _cwd(proc)),
                        model=infer_model(command),
                    )
                )
            except Exception:  # noqa: BLE001 — a process exiting mid-scan is normal
                log.debug("skipping process %s", pid, exc_info=True)

        out.sort(key=lambda p: p.gpu_mem_bytes, reverse=True)
        return out
