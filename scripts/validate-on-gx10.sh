#!/usr/bin/env bash
# Validate spark-dash-agent against real GB10 hardware.
#
# Checks the things that CANNOT be verified off-hardware — chiefly that unified
# memory is detected and that its numbers agree with nvidia-smi/free. Run this
# on a GX10 with the agent already listening.
#
#   ./scripts/validate-on-gx10.sh [agent_url]
#
# Read-only: it queries the agent and the system, and changes nothing.

set -uo pipefail

AGENT_URL="${1:-http://localhost:9500}"
PASS=0
FAIL=0
WARN=0

c_ok=$'\033[32m'; c_bad=$'\033[31m'; c_warn=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
ok()   { echo "${c_ok}PASS${c_off}  $1"; PASS=$((PASS+1)); }
bad()  { echo "${c_bad}FAIL${c_off}  $1"; FAIL=$((FAIL+1)); }
warn() { echo "${c_warn}WARN${c_off}  $1"; WARN=$((WARN+1)); }
info() { echo "${c_dim}      $1${c_off}"; }
hdr()  { echo; echo "── $1 ─────────────────────────────────────────"; }

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing required tool: $1"; exit 1; }; }
need curl
need python3

SNAP=$(curl -sf --max-time 10 "$AGENT_URL/snapshot" 2>/dev/null)
if [[ -z "$SNAP" ]]; then
  echo "${c_bad}Could not reach the agent at $AGENT_URL${c_off}"
  echo "Is it running?  docker ps | grep sparkdash"
  exit 1
fi

# Pull a value out of the snapshot by dotted path.
q() { printf '%s' "$SNAP" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for k in '$1'.split('.'):
    if d is None: break
    d = d.get(k) if isinstance(d,dict) else None
print('' if d is None else d)
"; }

echo "spark-dash-agent validation — $(q node_id) @ $AGENT_URL"

# ---------------------------------------------------------------- collectors
hdr "Collectors"
ERRORS=$(printf '%s' "$SNAP" | python3 -c "
import json,sys
for k,v in (json.load(sys.stdin).get('errors') or {}).items(): print(f'{k}: {v}')
")
if [[ -z "$ERRORS" ]]; then
  ok "all collectors reported"
else
  while IFS= read -r line; do
    # No GPU means nothing else can be trusted; other failures are partial.
    [[ "$line" == gpu:* ]] && bad "collector failed — $line" || warn "collector failed — $line"
  done <<< "$ERRORS"
fi

# ------------------------------------------------------------------- memory
# The headline check. Every standard GPU exporter gets this wrong on GB10.
hdr "Unified memory (the GB10 correctness check)"
UNIFIED=$(q memory.unified)
MEM_TOTAL=$(q memory.total_bytes)
MEM_USED=$(q memory.used_bytes)

if [[ "$UNIFIED" == "True" ]]; then
  ok "unified memory detected"
elif [[ -n "$MEM_TOTAL" ]]; then
  bad "unified memory NOT detected — NVML total disagreed with system total"
  info "the detection heuristic in collectors/memory.py needs revisiting"
else
  bad "no memory reading at all"
fi

if [[ -n "$MEM_TOTAL" && "$MEM_TOTAL" -gt 0 ]]; then
  info "total $(python3 -c "print(f'{$MEM_TOTAL/1024**3:.1f} GiB')")  used $(python3 -c "print(f'{$MEM_USED/1024**3:.1f} GiB ({100*$MEM_USED/$MEM_TOTAL:.0f}%)')")"

  # Cross-check against the kernel's own numbers.
  if [[ -r /proc/meminfo ]]; then
    KERNEL_TOTAL=$(awk '/^MemTotal:/ {print $2*1024}' /proc/meminfo)
    DELTA=$(python3 -c "print(abs($MEM_TOTAL-$KERNEL_TOTAL)/$KERNEL_TOTAL)")
    if python3 -c "import sys; sys.exit(0 if $DELTA < 0.02 else 1)"; then
      ok "total agrees with /proc/meminfo (within 2%)"
    else
      bad "total disagrees with /proc/meminfo — is the host /proc mounted?"
      info "agent $MEM_TOTAL vs kernel $KERNEL_TOTAL"
    fi
  fi
fi

if command -v nvidia-smi >/dev/null 2>&1; then
  SMI=$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader 2>/dev/null)
  if [[ -n "$SMI" ]]; then
    info "nvidia-smi compute apps:"
    while IFS= read -r line; do info "  $line"; done <<< "$SMI"
    info "^ compare against the process table below"
  else
    info "no GPU compute apps running — load a model for a fuller check"
  fi
fi

# ---------------------------------------------------------------------- GPU
hdr "GPU"
GPU_UTIL=$(q gpu.util_pct); CLOCK=$(q gpu.clock_mhz)
CLOCK_STATE=$(q gpu.clock_state); TEMP=$(q gpu.temp_c); POWER=$(q gpu.power_w)

if [[ -n "$GPU_UTIL" ]]; then
  ok "GPU telemetry present"
  info "util ${GPU_UTIL}%  clock ${CLOCK:-?}MHz [${CLOCK_STATE}]  temp ${TEMP:-?}°C  power ${POWER:-?}W"

  # Power is the value most likely to be wrong: nvitop reports milliwatts and
  # we divide. A GB10 idles ~5-25W and peaks well under 300W.
  if [[ -n "$POWER" ]]; then
    if python3 -c "import sys; sys.exit(0 if 0.5 < $POWER < 300 else 1)"; then
      ok "power draw is plausible (${POWER}W)"
    else
      bad "power draw implausible (${POWER}W) — check the mW→W conversion"
    fi
  fi

  # Load-gating: at idle the state must be IDLE, never THROTTLED.
  if python3 -c "import sys; sys.exit(0 if $GPU_UTIL < 30 else 1)"; then
    if [[ "$CLOCK_STATE" == "IDLE" ]]; then
      ok "clock state IDLE while unloaded (load-gating works)"
    else
      warn "clock state is $CLOCK_STATE at ${GPU_UTIL}% util — expected IDLE"
    fi
    info "re-run under inference load to check it reaches PASS"
  else
    case "$CLOCK_STATE" in
      PASS) ok "clock healthy under load (${CLOCK}MHz)" ;;
      THROTTLED) warn "THROTTLED at ${CLOCK}MHz under load"
                 info "either a real power-delivery issue, or the 1400MHz threshold needs calibrating" ;;
      LOCKED) info "clock externally capped (nvidia-smi -lgc?)" ;;
      *) warn "clock state $CLOCK_STATE at ${GPU_UTIL}% util" ;;
    esac
  fi
else
  bad "no GPU telemetry — NVML unavailable in the container?"
fi

# ------------------------------------------------------------------ processes
hdr "Process attribution"
PROCS=$(printf '%s' "$SNAP" | python3 -c "
import json,sys
for p in json.load(sys.stdin).get('processes') or []:
    print(f\"{p['pid']}\t{p['name']}\t{p['gpu_mem_bytes']/1024**3:.1f} GiB\t{p.get('runtime') or '-'}\")
")
if [[ -z "$PROCS" ]]; then
  info "no GPU processes running — load a model to validate this properly"
else
  UNRESOLVED=$(grep -c 'pid-' <<< "$PROCS" || true)
  if [[ "$UNRESOLVED" -gt 0 ]]; then
    bad "$UNRESOLVED process(es) unresolved — host PID namespace not visible"
    info "check 'pid: host' / --pid host took effect"
  else
    ok "process names resolved"
  fi
  while IFS= read -r line; do info "$line"; done <<< "$PROCS"
fi

# --------------------------------------------------------------------- PSI
hdr "Memory pressure (PSI)"
PSI_STATE=$(q psi.state)
if [[ -n "$PSI_STATE" ]]; then
  ok "PSI readable — state $PSI_STATE (some_avg10 $(q psi.some_avg10), full_avg10 $(q psi.full_avg10))"
  info "PSI bands are UNCALIBRATED guesses; see thresholds.py"
else
  warn "no PSI reading — kernel built without CONFIG_PSI, or /proc not mounted from host"
fi

# ------------------------------------------------------------------ runtimes
hdr "Inference runtimes"
ROUTERS=$(printf '%s' "$SNAP" | python3 -c "
import json,sys
for r in (json.load(sys.stdin).get('runtimes') or {}).get('llama_cpp') or []:
    print(f\"{r['name'] or r['endpoint']}\t{r['reachable']}\t{r['known_model_count']}\t{len(r['loaded_models'] or [])}\")
")
if [[ -z "$ROUTERS" ]]; then
  info "no llama.cpp routers configured (LLAMA_ROUTER_URLS unset) — skipping"
else
  UNRECOGNIZED=0
  while IFS=$'\t' read -r rname reachable known loaded; do
    if [[ "$reachable" != "True" ]]; then
      bad "router $rname unreachable"
      continue
    fi
    ok "router $rname — $known known model(s), $loaded loaded"
    [[ "$known" -gt 0 && "$loaded" -eq 0 ]] && UNRECOGNIZED=1
  done <<< "$ROUTERS"

  if [[ "$UNRECOGNIZED" -eq 1 ]]; then
    info "a router reports models but none loaded. Either nothing is loaded"
    info "(fine), or the /v1/models shape wasn't recognized and we fell back"
    info "to 'not loaded'. Confirm with the diagnose command below."
  fi
  echo
  echo "  ${c_warn}THE CRITICAL CHECK${c_off} — leave the agent running for ~10 minutes with"
  echo "  models idle, then confirm no model loaded itself. If idle models wake up,"
  echo "  stop the agent and set LLAMA_SCRAPE_LOADED_MODEL_METRICS=false."
  echo "  (autoload bug: ggml-org/llama.cpp#23096)"
fi

VLLM_COUNT=$(printf '%s' "$SNAP" | python3 -c "
import json,sys
print(len((json.load(sys.stdin).get('runtimes') or {}).get('vllm') or []))
")
[[ "$VLLM_COUNT" -gt 0 ]] && ok "$VLLM_COUNT vLLM instance(s) scraped" \
                          || info "no vLLM instances (VLLM_URLS unset?)"

# ------------------------------------------------------------------- summary
hdr "Summary"
echo "  ${c_ok}$PASS passed${c_off}   ${c_warn}$WARN warnings${c_off}   ${c_bad}$FAIL failures${c_off}"
echo
echo "  Full snapshot:   curl -s $AGENT_URL/snapshot | jq"
echo "  Prometheus view: curl -s $AGENT_URL/metrics"
[[ $FAIL -gt 0 ]] && exit 1 || exit 0
