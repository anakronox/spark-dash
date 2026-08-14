#!/usr/bin/env bash
# Push a synthetic alert through Alertmanager to verify notification delivery.
#
#   ./scripts/test-alert.sh [warning|critical]
#
# Run on the monitoring VM.
#
# Why this exists: a `curl -d` straight to the ntfy topic proves the topic and
# your app subscription work, but nothing else. It doesn't exercise the parts
# that actually break — whether Alertmanager read the url_file, whether routing
# matched a receiver, or whether the webhook fires. This posts a real alert to
# Alertmanager's API so the whole path runs.
#
# The alert auto-resolves after 2 minutes, which also tests that resolved
# notifications arrive.

set -euo pipefail

SEVERITY="${1:-warning}"
AM="${ALERTMANAGER_URL:-http://localhost:9093}"

c_ok=$'\033[32m'; c_bad=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

case "$SEVERITY" in
  warning|critical) ;;
  *) echo "usage: $0 [warning|critical]"; exit 2 ;;
esac

if ! curl -sf --max-time 5 "$AM/-/healthy" >/dev/null; then
  echo "${c_bad}Alertmanager not responding at $AM${c_off}"
  echo "  docker compose ps"
  echo "  docker logs sparkdash-alertmanager --tail 20"
  exit 1
fi

# Confirm a receiver is actually wired up before sending — otherwise a silent
# non-delivery looks identical to a broken topic.
if ! curl -s "$AM/api/v2/status" | grep -q 'url_file\|url'; then
  echo "${c_bad}No webhook receiver in the loaded config.${c_off}"
  echo "Alertmanager may have started before the secrets file existed."
  exit 1
fi

STARTS="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
ENDS="$(date -u -d '+2 minutes' +%Y-%m-%dT%H:%M:%S.000Z 2>/dev/null \
        || date -u -v+2M +%Y-%m-%dT%H:%M:%S.000Z)"

echo "Sending a ${SEVERITY} test alert to $AM"

curl -sf -X POST "$AM/api/v2/alerts" \
  -H 'Content-Type: application/json' \
  -d "[{
    \"labels\": {
      \"alertname\": \"SparkDashTestAlert\",
      \"severity\": \"${SEVERITY}\",
      \"node\": \"test\"
    },
    \"annotations\": {
      \"summary\": \"Test alert from spark-dash (${SEVERITY})\",
      \"description\": \"Synthetic alert verifying the Alertmanager to ntfy path. Auto-resolves in 2 minutes; a resolved notification should follow.\"
    },
    \"startsAt\": \"${STARTS}\",
    \"endsAt\": \"${ENDS}\"
  }]" > /dev/null

echo "${c_ok}accepted${c_off}"
echo
# Critical routes with group_wait 10s, warning with 45s.
if [[ "$SEVERITY" == "critical" ]]; then
  echo "Expect a notification in ~10s (critical skips most batching)."
else
  echo "Expect a notification in ~45s (warning batches first)."
fi
echo "Then a RESOLVED notification about 2 minutes later."
echo
echo "${c_dim}Meanwhile:${c_off}"
echo "  curl -s $AM/api/v2/alerts | jq '.[].labels.alertname'"
echo "  docker logs sparkdash-alertmanager --tail 20"
echo
echo "${c_dim}If nothing arrives, the logs will say why — usually a trailing${c_off}"
echo "${c_dim}newline in the secrets file, or the app subscribed to a different topic.${c_off}"
