#!/usr/bin/env bash
# Create the host directories this stack bind-mounts, with the ownership its
# containers need.
#
#   sudo ./prepare-host.sh [data_root]
#
# Run once before the first `docker compose up`.
#
# Why this is needed: the stack uses bind mounts rather than named volumes, so
# persistent data lives at a visible, backup-able path instead of somewhere
# under /var/lib/docker. The tradeoff is that Docker no longer seeds ownership
# for you — a root-owned directory bind-mounted into a container running as a
# non-root user is simply not writable, and the container will crash-loop with
# a permission error that doesn't obviously point here.

set -euo pipefail

DATA_ROOT="${1:-/docker/spark-dash-stack-central}"

# Prometheus's official image runs as nobody.
PROMETHEUS_UID=65534
PROMETHEUS_GID=65534
# Matches the non-root user in backend/Dockerfile.
BACKEND_UID=10002
BACKEND_GID=10002

c_ok=$'\033[32m'; c_bad=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

if [[ "$(id -u)" -ne 0 ]]; then
  echo "${c_bad}Needs root${c_off} to set directory ownership."
  echo "  sudo $0 ${DATA_ROOT}"
  exit 1
fi

echo "Preparing ${DATA_ROOT}"

# Prometheus TSDB. This is the one directory that grows — it's what retention
# settings act on, and what you'd back up or watch for disk usage.
mkdir -p "${DATA_ROOT}/prometheus"
chown -R "${PROMETHEUS_UID}:${PROMETHEUS_GID}" "${DATA_ROOT}/prometheus"
echo "${c_ok}✓${c_off} ${DATA_ROOT}/prometheus  ${c_dim}(uid ${PROMETHEUS_UID}, Prometheus TSDB)${c_off}"

# Scrape targets the backend renders from SPARK_NODES. Written by the backend,
# read by Prometheus — so it must be writable by the backend's user.
mkdir -p "${DATA_ROOT}/targets"
chown -R "${BACKEND_UID}:${BACKEND_GID}" "${DATA_ROOT}/targets"
echo "${c_ok}✓${c_off} ${DATA_ROOT}/targets     ${c_dim}(uid ${BACKEND_UID}, generated scrape targets)${c_off}"

echo
echo "Done. Now:"
echo "  docker compose up -d"
echo
echo "${c_dim}If you set DATA_ROOT in .env to something other than the default,${c_off}"
echo "${c_dim}pass the same path to this script.${c_off}"
