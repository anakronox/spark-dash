#!/usr/bin/env bash
# Build the spark-dash images on this host.
#
#   ./scripts/build-images.sh agent      # run this ON a GX10 (arm64)
#   ./scripts/build-images.sh backend    # run this ON whatever will run it
#   ./scripts/build-images.sh both       # only correct if one host runs both
#   ./scripts/build-images.sh agent --tag rc1
#
# THIS IS THE SCRIPT AN END USER RUNS, and it is the whole job: it touches no
# registry, needs no `docker login`, and produces exactly the image names the
# compose files default to. Publishing to a registry is a separate, optional,
# maintainer-shaped step — see publish-images.sh.
#
# That split exists because the default used to be backwards. Building was
# `publish-images.sh <target> --no-push`: a script called *publish* with a flag
# saying *do not publish*, which failed a first-time user against a registry
# they have no account on if they forgot the flag.
#
# WHY NATIVE RATHER THAN CROSS-BUILDING: the GX10s are arm64 and a monitoring
# VM is typically amd64. Building each image where it will run means no QEMU
# emulation, no buildx setup, and a build that takes seconds instead of many
# minutes. On a single-host install both images are built here and both are
# arm64, which is exactly right — see docs/deployment.md#single-host.

set -euo pipefail

c_ok=$'\033[32m'; c_bad=$'\033[31m'; c_warn=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

usage() {
  cat <<'EOF'
usage: build-images.sh {agent|backend|both} [options]

  agent      build on a GX10 (arm64)
  backend    build on whatever host will run it
  both       build both here — correct on a single-host install

options:
  --tag TAG        override the tag (default: short git sha)
  --keep N         sha-tagged images to keep locally (default 5, 0 = keep all)
  --print-tag      print the tag this run would use, and exit
  -h, --help       this

Produces `spark-dash-agent:{TAG,latest}` / `spark-dash-backend:{TAG,latest}`
locally. The compose files default to those exact names, so `up -d` runs what
this built with nothing else to configure.

To push to a registry instead, see publish-images.sh — a maintainer path that
most installs never need.
EOF
}

TARGET=""; TAG_OVERRIDE=""; KEEP="${KEEP_IMAGES:-5}"; PRINT_TAG=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    agent|backend|both) TARGET="$1" ;;
    --tag)        TAG_OVERRIDE="${2:-}"; shift ;;
    --keep)       KEEP="${2:-5}"; shift ;;
    --print-tag)  PRINT_TAG=1 ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "${c_bad}unknown argument:${c_off} $1"; echo; usage; exit 2 ;;
  esac
  shift
done

# --- tag --------------------------------------------------------------------
#
# The commit, so a deployed image can be traced back to source. A dirty tree is
# marked, because an image built from uncommitted changes cannot be rebuilt
# from git — and that is exactly the image you least want to find running
# somewhere six weeks later with no way to reproduce it.
#
# COMPUTED HERE AND NOWHERE ELSE. publish-images.sh asks for it with
# --print-tag rather than deriving its own: two scripts computing "the same"
# tag independently is how you publish an image that is not the one you built.
if [[ -n "$TAG_OVERRIDE" ]]; then
  TAG="$TAG_OVERRIDE"
else
  TAG="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
  if ! git diff-index --quiet HEAD -- 2>/dev/null; then
    TAG="${TAG}-dirty"
    [[ $PRINT_TAG -eq 0 ]] && \
      echo "${c_warn}WARNING${c_off} working tree is dirty; tagging as ${TAG}" >&2
  fi
fi

if [[ $PRINT_TAG -eq 1 ]]; then echo "$TAG"; exit 0; fi
if [[ -z "$TARGET" ]]; then usage; exit 2; fi

# Every build leaves a sha-tagged image behind, and `docker image prune` will
# NOT reclaim them — they are tagged, not dangling. At ~160MB each that is
# roughly a gigabyte per thirty builds, growing forever and silently.
#
# Keep the newest few so a rollback can retag a recent build instead of
# rebuilding it, and drop the rest. `docker images` lists newest first, so the
# tail of that list is what ages out. An image still backing a container refuses
# to be removed, which is the correct outcome — skip it rather than force.
prune_old_tags() {
  local base="$1"
  [[ "$KEEP" -le 0 ]] && return 0

  local stale=()
  mapfile -t stale < <(docker images "$base" --format '{{.Tag}}' \
                       | grep -v '^latest$' | tail -n +$((KEEP + 1)))
  [[ ${#stale[@]} -eq 0 ]] && return 0

  local removed=0
  for tag in "${stale[@]}"; do
    if docker rmi "${base}:${tag}" >/dev/null 2>&1; then removed=$((removed + 1)); fi
  done
  [[ $removed -gt 0 ]] && echo "${c_dim}pruned ${removed} old image(s), kept the newest ${KEEP}${c_off}"
  return 0
}

build() {
  local name="$1" dockerfile="$2" env_var="$3"

  echo
  echo "── ${name} ─────────────────────────────────────"
  echo "${c_dim}arch: $(uname -m)   tag: ${TAG}${c_off}"

  # Build context is the repo ROOT, not the component directory — both images
  # depend on the local common/ package, which has to be inside the context.
  #
  # BUILD_VERSION is what the running container reports as its version. Without
  # it a stale container presents as a missing feature rather than as a stale
  # container, which has cost real debugging time here more than once.
  docker build -f "$dockerfile" --build-arg "BUILD_VERSION=${TAG}" \
    -t "${name}:${TAG}" -t "${name}:latest" .

  echo "${c_ok}built${c_off} ${name}:${TAG} ${c_dim}(and :latest)${c_off}"
  echo "${c_dim}this is the stacks' default image, so 'up -d' runs it as-is,${c_off}"
  echo "${c_dim}provided ${env_var} and PULL_POLICY are unset in .env.${c_off}"
  prune_old_tags "$name"
}

case "$TARGET" in
  agent)   build spark-dash-agent   agent/Dockerfile   AGENT_IMAGE ;;
  backend) build spark-dash-backend backend/Dockerfile BACKEND_IMAGE ;;
  both)
    echo "${c_warn}Note:${c_off} both images will be built for $(uname -m). That is correct on"
    echo "a single-host install, where one GB10 runs both stacks; on a split"
    echo "deployment each image belongs on a different architecture."
    build spark-dash-agent   agent/Dockerfile   AGENT_IMAGE
    build spark-dash-backend backend/Dockerfile BACKEND_IMAGE
    ;;
esac

echo
echo "Built locally. The image exists only on this host's docker daemon, which"
echo "is all the stacks need: they default to this name and do not pull."
