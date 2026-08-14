#!/usr/bin/env bash
# Build and push spark-dash images to the Forgejo container registry.
#
#   ./scripts/publish-images.sh agent      # run this ON a GX10 (arm64)
#   ./scripts/publish-images.sh backend    # run this ON the monitoring VM (amd64)
#   ./scripts/publish-images.sh both
#
# Run from a clone of this repo, on a host of the TARGET ARCHITECTURE.
#
# Why native rather than cross-building: the GX10s are arm64 and the monitoring
# VM is almost certainly amd64. Building each image where it will run means no
# QEMU emulation, no buildx multi-arch setup, and a build that takes seconds
# instead of many minutes. The tradeoff is that you build in two places — which
# is fine, because each image is only ever deployed to one of them.

set -euo pipefail

REGISTRY="${REGISTRY:-forgejo.indielab.tech}"
OWNER="${OWNER:-brian}"

c_ok=$'\033[32m'; c_bad=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Tag with the commit so a deployed image can be traced back to source, plus
# :latest for convenience. A dirty tree is marked, because an image built from
# uncommitted changes cannot be reproduced from git.
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
if ! git diff-index --quiet HEAD -- 2>/dev/null; then
  GIT_SHA="${GIT_SHA}-dirty"
  echo "${c_bad}WARNING${c_off} working tree is dirty; tagging as ${GIT_SHA}"
fi

build_and_push() {
  local name="$1" dockerfile="$2" env_var="$3"
  local base="${REGISTRY}/${OWNER}/${name}"

  echo
  echo "── ${name} ─────────────────────────────────────"
  echo "${c_dim}arch: $(uname -m)   tag: ${GIT_SHA}${c_off}"

  # Build context is the repo ROOT, not the component directory — both images
  # depend on the local common/ package.
  docker build -f "$dockerfile" \
    --build-arg "BUILD_VERSION=${GIT_SHA}" \
    -t "${base}:${GIT_SHA}" -t "${base}:latest" .

  docker push "${base}:${GIT_SHA}"
  docker push "${base}:latest"

  echo "${c_ok}pushed${c_off} ${base}:${GIT_SHA}"
  echo "${c_dim}pin this in the stack's .env:${c_off}"
  echo "  ${env_var}=${base}:${GIT_SHA}"
}

check_login() {
  # Docker has no "am I logged in" command; the config file is the only signal.
  if ! grep -q "$REGISTRY" ~/.docker/config.json 2>/dev/null; then
    echo "${c_bad}Not logged in to ${REGISTRY}${c_off}"
    echo "Run:  docker login ${REGISTRY}"
    echo "Use a Forgejo access token with package read/write scope as the password."
    exit 1
  fi
}

TARGET="${1:-}"
case "$TARGET" in
  agent)
    check_login
    build_and_push spark-dash-agent agent/Dockerfile AGENT_IMAGE
    ;;
  backend)
    check_login
    build_and_push spark-dash-backend backend/Dockerfile BACKEND_IMAGE
    ;;
  both)
    check_login
    echo "${c_dim}Note: both images will be built for $(uname -m). The agent must be${c_off}"
    echo "${c_dim}arm64 (GX10) and the backend whatever the monitoring VM runs.${c_off}"
    build_and_push spark-dash-agent agent/Dockerfile AGENT_IMAGE
    build_and_push spark-dash-backend backend/Dockerfile BACKEND_IMAGE
    ;;
  *)
    echo "usage: $0 {agent|backend|both}"
    echo
    echo "  agent    build on a GX10 (arm64)"
    echo "  backend  build on the monitoring VM (amd64)"
    exit 2
    ;;
esac

echo
echo "Dockhand will redeploy on the next git change to the stack repo."
echo "To roll a new image out immediately, pin the new tag in the stack's .env"
echo "and commit — that's the change Dockhand watches for."
