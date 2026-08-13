#!/usr/bin/env bash
# Prove the agent never wakes a sleeping llama.cpp model.
#
#   ./scripts/soak-test-autoload.sh [minutes] [agent_url]
#
# Why this exists: the agent builds snapshots ON DEMAND. An idle daemon that
# nobody polls never contacts the routers at all, so "no models loaded" after
# leaving it running proves nothing. This polls continuously — harder than
# Prometheus and the live view combined — and reports any model that changes
# state.
#
# A model going sleeping -> active with no inference traffic means
# GET /metrics?model= autoloaded it (ggml-org/llama.cpp#23096), and the agent's
# core safety property is broken. Fix: set
# LLAMA_SCRAPE_LOADED_MODEL_METRICS=false.
#
# Read-only. Run it while NOT sending inference requests, or a genuine load
# will look like a false positive.

set -uo pipefail

MINUTES="${1:-10}"
AGENT_URL="${2:-http://localhost:9500}"
INTERVAL=2

c_ok=$'\033[32m'; c_bad=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

states() {
  curl -sf --max-time 5 "$AGENT_URL/snapshot" 2>/dev/null | python3 -c "
import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for r in (d.get('runtimes') or {}).get('llama_cpp') or []:
    label = r['name'] or r['endpoint']
    for m in r['models'] or []:
        print(f\"{label}/{m['name']}\t{m['state']}\")
"
}

BASELINE=$(states)
if [[ -z "$BASELINE" ]]; then
  echo "${c_bad}No router models reported.${c_off}"
  echo "Is LLAMA_ROUTER_URLS set on the container, and are the routers reachable?"
  echo "  docker logs sparkdash-agent --tail 20"
  exit 1
fi

TOTAL=$(wc -l <<< "$BASELINE" | tr -d ' ')
END=$(( $(date +%s) + MINUTES * 60 ))
POLLS=0
VIOLATIONS=0

echo "Soak test: ${MINUTES}m, polling every ${INTERVAL}s — harder than production will."
echo
echo "Baseline:"
while IFS=$'\t' read -r model state; do
  printf '  %-48s %s\n' "$model" "$state"
done <<< "$BASELINE"
echo
echo "${c_dim}Watching for state changes. Do not send inference requests.${c_off}"
echo "${c_dim}Ctrl-C to stop early.${c_off}"
echo

while [[ $(date +%s) -lt $END ]]; do
  sleep "$INTERVAL"
  CURRENT=$(states)
  POLLS=$((POLLS + 1))
  [[ -z "$CURRENT" ]] && continue

  # Compare against baseline; report only transitions.
  while IFS=$'\t' read -r model state; do
    # Exact field match rather than a regex — model names contain dots and
    # dashes that would need escaping, and a mis-escaped pattern would silently
    # match nothing and hide a real violation.
    was=$(awk -F'\t' -v m="$model" '$1 == m {print $2; exit}' <<< "$BASELINE")
    [[ -z "$was" || "$was" == "$state" ]] && continue

    if [[ "$state" == "active" && "$was" != "active" ]]; then
      echo "${c_bad}VIOLATION${c_off} $(date +%H:%M:%S)  $model  $was -> $state"
      echo "          a model woke with no inference traffic — autoload triggered"
      VIOLATIONS=$((VIOLATIONS + 1))
    else
      echo "${c_dim}change${c_off}    $(date +%H:%M:%S)  $model  $was -> $state"
    fi
  done <<< "$CURRENT"

  BASELINE="$CURRENT"
  REMAIN=$(( (END - $(date +%s)) / 60 ))
  printf '\r%s' "${c_dim}polls: $POLLS   ~${REMAIN}m left${c_off}"
done

printf '\r%*s\r' 40 ''
echo
if [[ $VIOLATIONS -eq 0 ]]; then
  echo "${c_ok}PASS${c_off} — $POLLS polls over ${MINUTES}m, $TOTAL model(s), none woke."
  echo "     The agent reads router state without disturbing eviction."
else
  echo "${c_bad}FAIL${c_off} — $VIOLATIONS model(s) woke during polling."
  echo "     Autoload reaches further than expected. Restart the agent with:"
  echo "       -e LLAMA_SCRAPE_LOADED_MODEL_METRICS=false"
  exit 1
fi
