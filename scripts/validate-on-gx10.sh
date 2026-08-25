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

hdr "Node health"
HEALTH=$(q health)
REASONS=$(printf '%s' "$SNAP" | python3 -c "
import json,sys
print('; '.join(json.load(sys.stdin).get('health_reasons') or []))
")
case "$HEALTH" in
  good)     ok "health: good" ;;
  warning)  warn "health: warning — $REASONS" ;;
  serious)  warn "health: serious — $REASONS" ;;
  critical) bad "health: critical — $REASONS" ;;
  *)        warn "health: $HEALTH" ;;
esac

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

  # Sustained load is measured in wall-clock seconds, so a single poll can
  # never report anything but IDLE. Poll for a few seconds before judging.
  if python3 -c "import sys; sys.exit(0 if $GPU_UTIL >= 30 else 1)"; then
    info "GPU is loaded — sampling for 8s to let sustained-load detection settle"
    for _ in 1 2 3 4 5 6 7 8; do
      sleep 1
      SNAP2=$(curl -sf --max-time 5 "$AGENT_URL/snapshot" 2>/dev/null)
      [[ -n "$SNAP2" ]] && SNAP="$SNAP2"
    done
    GPU_UTIL=$(q gpu.util_pct); CLOCK=$(q gpu.clock_mhz); CLOCK_STATE=$(q gpu.clock_state)
    info "after settling: util ${GPU_UTIL}%  clock ${CLOCK}MHz [${CLOCK_STATE}]"
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

  UNLABELED=$(awk -F'\t' '$4=="-"' <<< "$PROCS" | wc -l | tr -d ' ')
  if [[ "$UNLABELED" -gt 0 ]]; then
    warn "$UNLABELED process(es) have no runtime label"
    info "run the diagnose command below to see what name/cmdline/cwd report"
  fi

  # Non-LLM workloads share the same unified pool as the models, so their
  # footprint is real capacity pressure, not a footnote.
  printf '%s' "$SNAP" | python3 -c "
import json,sys
LLM={'vllm','llama.cpp','sglang','atlas','tgi','ollama'}
procs=json.load(sys.stdin).get('processes') or []
llm=sum(p['gpu_mem_bytes'] for p in procs if p.get('runtime') in LLM)
other=sum(p['gpu_mem_bytes'] for p in procs if p.get('runtime') not in LLM)
if other:
    print(f'      GPU memory: {llm/1024**3:.1f} GiB LLM runtimes, {other/1024**3:.1f} GiB other workloads')
    print('      (other workloads compete for the same unified pool)')
"
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

# ------------------------------------------------------------------ thermal
# THE CHECK THAT CANNOT BE WRITTEN AS A UNIT TEST. The collector classifies
# every chip the machine exposes; whether it saw them ALL, and saw each of them
# once, is only answerable against real sysfs on real hardware. node_exporter
# walks the same trees independently, so it is the second opinion.
hdr "Temperature sensors"
THERMAL=$(printf '%s' "$SNAP" | python3 -c "
import json,sys
for t in json.load(sys.stdin).get('temperatures') or []:
    print(f\"{t['domain']}\t{t['sensor']}\t{t['celsius']}\t{t.get('limit_c')}\")
")
if [[ -z "$THERMAL" ]]; then
  bad "the agent reports no temperature sensors — is /sys mounted from the host?"
else
  N=$(printf '%s\n' "$THERMAL" | wc -l | tr -d ' ')
  UNIQ=$(printf '%s\n' "$THERMAL" | cut -f2 | sort -u | wc -l | tr -d ' ')
  if [[ "$N" == "$UNIQ" ]]; then
    ok "$N sensors, all distinctly named"
  else
    bad "$N sensors but only $UNIQ distinct names — a chip is being read twice"
  fi

  # The double-count guard, checked against the kernel rather than the fixture:
  # hwmon0 IS thermal_zone0's hwmon child and republishes all seven zones, so a
  # package count above the zone count means the skip stopped working.
  ZONES=$(ls -d /sys/class/thermal/thermal_zone* 2>/dev/null | wc -l | tr -d ' ')
  PKG=$(printf '%s\n' "$THERMAL" | awk -F'\t' '$1=="package"' | wc -l | tr -d ' ')
  if [[ "$PKG" == "$ZONES" ]]; then
    ok "package sensors match the kernel's zone count ($ZONES)"
  else
    bad "$PKG package sensors against $ZONES thermal zones — acpitz is being double-read"
  fi

  # Second opinion. node_exporter walks the same sysfs independently, so a
  # sensor it has and the agent does not is one the classifier dropped.
  if NE=$(curl -sf --max-time 10 http://127.0.0.1:9100/metrics 2>/dev/null); then
    CMP=$(printf '%s' "$NE" | python3 -c "
import re,sys
raw=sys.stdin.read()
zones=hw=0
for line in raw.splitlines():
    if line.startswith('node_thermal_zone_temp{'): zones+=1
    elif line.startswith('node_hwmon_temp_celsius{'):
        chip=re.search(r'chip=\"([^\"]*)\"', line)
        # node_exporter republishes the zones under hwmon too; those are the
        # duplicates the agent deliberately skips.
        if chip and not chip.group(1).startswith('thermal_'): hw+=1
print(zones+hw)
")
    if [[ "$CMP" == "$N" ]]; then
      ok "agrees with node_exporter on the sensor count ($CMP)"
    else
      warn "agent sees $N sensors, node_exporter sees $CMP — one of them is missing a chip"
    fi
  else
    info "node_exporter not reachable on :9100; skipped the second opinion"
  fi

  HOT=$(printf '%s\n' "$THERMAL" | sort -t$'\t' -k3 -gr | head -1)
  info "hottest: $(printf '%s' "$HOT" | cut -f2) at $(printf '%s' "$HOT" | cut -f3)C"
  NOLIMIT=$(printf '%s\n' "$THERMAL" | awk -F'\t' '$4=="None"' | wc -l | tr -d ' ')
  [[ "$NOLIMIT" == "0" ]] && ok "every sensor states a limit" \
    || info "$NOLIMIT sensors state no limit — expected for wifi and the nvme spare sensors"
fi

# ------------------------------------------------------------------ runtimes
hdr "Inference runtimes"
ROUTERS=$(printf '%s' "$SNAP" | python3 -c "
import json,sys
for r in (json.load(sys.stdin).get('runtimes') or {}).get('llama_cpp') or []:
    models = r['models'] or []
    counts = {}
    for m in models:
        counts[m['state']] = counts.get(m['state'], 0) + 1
    summary = ', '.join(f'{v} {k}' for k, v in sorted(counts.items())) or 'none'
    unknown = sum(1 for m in models if m['state'] == 'unknown')
    raws = ';'.join(sorted({m['raw_status'] for m in models if m['state'] == 'unknown'}))
    print(f\"{r['name'] or r['endpoint']}\t{r['reachable']}\t{len(models)}\t{r.get('max_instances') or '?'}\t{summary}\t{unknown}\t{raws}\")
")
if [[ -z "$ROUTERS" ]]; then
  info "no llama.cpp routers configured (LLAMA_ROUTER_URLS unset) — skipping"
else
  while IFS=$'\t' read -r rname reachable total maxinst summary unknown raws; do
    if [[ "$reachable" != "True" ]]; then
      bad "router $rname unreachable"
      continue
    fi
    ok "router $rname — $total model(s), max_instances $maxinst"
    info "states: $summary"
    if [[ "$unknown" -gt 0 ]]; then
      warn "$unknown model(s) in UNKNOWN state (raw: $raws)"
      info "unrecognized status values are never scraped (fail-safe), but the"
      info "mapping in collectors/llama_router.py should learn them"
    fi
  done <<< "$ROUTERS"

  echo
  SCRAPE_ON=$(printf '%s' "$SNAP" | python3 -c "
import json,sys
print('yes' if any(m.get('tokens_per_sec') for r in ((json.load(sys.stdin).get('runtimes') or {}).get('llama_cpp') or []) for m in (r['models'] or [])) else 'no')
" 2>/dev/null || echo "no")
  info "per-model metrics scraping is opt-in per router via LLAMA_METRICS_ROUTERS"
  info "(unset = no /metrics?model= request is ever issued to any router)"
  echo
  echo "  ${c_warn}PROVE IT${c_off} — run the soak test, which polls continuously and"
  echo "  reports any model that wakes:"
  echo "    ./scripts/soak-test-autoload.sh 10"
fi

# Every engine the snapshot carries, not vLLM alone — a node running SGLang
# with nothing configured for it is the same silence, and reporting only on
# vLLM would have this script agree with a half-configured node.
ENGINE_REPORT=$(printf '%s' "$SNAP" | python3 -c "
import json,sys
runtimes=json.load(sys.stdin).get('runtimes') or {}
for name in ('vllm','sglang'):
    print(f\"{name} {len(runtimes.get(name) or [])}\")
")
while read -r ENGINE COUNT; do
  ENV_VAR=$(printf '%s' "$ENGINE" | tr '[:lower:]' '[:upper:]')_URLS
  [[ "$COUNT" -gt 0 ]] && ok "$COUNT $ENGINE instance(s) scraped" \
                       || info "no $ENGINE instances (${ENV_VAR} unset?)"
done <<< "$ENGINE_REPORT"

# ------------------------------------------------------------------- summary
hdr "Summary"
echo "  ${c_ok}$PASS passed${c_off}   ${c_warn}$WARN warnings${c_off}   ${c_bad}$FAIL failures${c_off}"
echo
echo "  Full snapshot:   curl -s $AGENT_URL/snapshot | jq"
echo "  Prometheus view: curl -s $AGENT_URL/metrics"
[[ $FAIL -gt 0 ]] && exit 1 || exit 0
