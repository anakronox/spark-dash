#!/usr/bin/env bash
# Build and push spark-dash images to a container registry.
#
#   ./scripts/publish-images.sh agent      # run this ON a GX10 (arm64)
#   ./scripts/publish-images.sh backend    # run this ON the monitoring VM (amd64)
#   ./scripts/publish-images.sh both
#   ./scripts/publish-images.sh backend --no-push     # build locally, no registry
#   ./scripts/publish-images.sh agent --tag rc1
#   ./scripts/publish-images.sh --help
#
# Run from a clone of this repo, on a host of the TARGET ARCHITECTURE.
#
# WHY NATIVE RATHER THAN CROSS-BUILDING: the GX10s are arm64 and the monitoring
# VM is amd64. Building each image where it will run means no QEMU emulation, no
# buildx multi-arch setup, and a build that takes seconds instead of many
# minutes. The tradeoff is that you build in two places — which is fine, because
# each image is only ever deployed to one of them.
#
# WHY THIS SCRIPT RATHER THAN BUILD-ON-DEPLOY. Deploy tooling can often build
# from a Dockerfile at deploy time. Deliberately not used here — see
# docs/deployment.md. Two reasons: rollback stays a one-line tag edit with no
# rebuild, and the build happens once rather than once per host, so every node
# runs bytes that are known to be identical.

set -euo pipefail

c_ok=$'\033[32m'; c_bad=$'\033[31m'; c_warn=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

usage() {
  cat <<'EOF'
usage: publish-images.sh {agent|backend|both} [options]

  agent      build on a GX10 (arm64)
  backend    build on the monitoring VM (amd64)
  both       build both here — only correct if one host runs both

options:
  --no-push        build locally and stop; no registry or login needed
  --tag TAG        override the tag (default: short git sha)
  --no-latest      push only the tag, not :latest
  -h, --help       this

options (cont.):
  --keep N         sha-tagged images to keep locally (default 5, 0 = keep all)

environment:
  REGISTRY   registry host   (default: derived from git remote origin)
  OWNER      registry owner  (default: derived from git remote origin)

Deriving both from the clone's own remote means a fork publishes to its own
registry with no configuration, and no one's personal registry is baked into
a tracked file. Override either when the registry is not where the source
lives.
EOF
}

# --- registry identity ------------------------------------------------------
#
# Defaults come from wherever THIS clone was cloned from. Handles the three
# remote spellings git uses: https://host/owner/repo.git, ssh://git@host/...,
# and the scp-style git@host:owner/repo.git.
derive_from_remote() {
  local url
  url="$(git config --get remote.origin.url 2>/dev/null || true)"
  [[ -z "$url" ]] && return 0

  url="${url%.git}"
  url="${url#*://}"      # strip scheme if present
  url="${url#*@}"        # strip user@ if present
  url="${url/://}"       # scp-style host:owner -> host/owner

  DERIVED_REGISTRY="${url%%/*}"
  local rest="${url#*/}"
  DERIVED_OWNER="${rest%%/*}"
}

DERIVED_REGISTRY=""; DERIVED_OWNER=""
derive_from_remote
REGISTRY="${REGISTRY:-$DERIVED_REGISTRY}"
OWNER="${OWNER:-$DERIVED_OWNER}"

# --- arguments --------------------------------------------------------------
TARGET=""; PUSH=1; PUSH_LATEST=1; TAG_OVERRIDE=""; KEEP="${KEEP_IMAGES:-5}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    agent|backend|both) TARGET="$1" ;;
    --no-push)    PUSH=0 ;;
    --no-latest)  PUSH_LATEST=0 ;;
    --tag)        TAG_OVERRIDE="${2:-}"; shift ;;
    --keep)       KEEP="${2:-5}"; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "${c_bad}unknown argument:${c_off} $1"; echo; usage; exit 2 ;;
  esac
  shift
done

if [[ -z "$TARGET" ]]; then usage; exit 2; fi

if [[ $PUSH -eq 1 && ( -z "$REGISTRY" || -z "$OWNER" ) ]]; then
  echo "${c_bad}Cannot work out where to push.${c_off}"
  echo "No usable 'origin' remote, so REGISTRY and OWNER must be set:"
  echo
  echo "  REGISTRY=registry.example.com OWNER=you $0 $TARGET"
  echo
  echo "Or build without a registry:  $0 $TARGET --no-push"
  exit 2
fi

# --- tag --------------------------------------------------------------------
#
# The commit, so a deployed image can be traced back to source. A dirty tree is
# marked, because an image built from uncommitted changes cannot be rebuilt
# from git — and that is exactly the image you least want to find running
# somewhere six weeks later with no way to reproduce it.
if [[ -n "$TAG_OVERRIDE" ]]; then
  TAG="$TAG_OVERRIDE"
else
  TAG="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
  if ! git diff-index --quiet HEAD -- 2>/dev/null; then
    TAG="${TAG}-dirty"
    echo "${c_warn}WARNING${c_off} working tree is dirty; tagging as ${TAG}"
  fi
fi

build_and_push() {
  local name="$1" dockerfile="$2" env_var="$3"
  local base
  # A local build is named for what will RUN it, not for where it isn't going.
  # The compose files default to the bare name, so --no-push must produce that
  # exact tag or a fresh clone builds an image its own stack cannot see.
  if [[ $PUSH -eq 1 && -n "$REGISTRY" && -n "$OWNER" ]]; then
    base="${REGISTRY}/${OWNER}/${name}"
  else
    base="$name"
  fi

  echo
  echo "── ${name} ─────────────────────────────────────"
  echo "${c_dim}arch: $(uname -m)   tag: ${TAG}${c_off}"

  local tags=(-t "${base}:${TAG}")
  [[ $PUSH_LATEST -eq 1 ]] && tags+=(-t "${base}:latest")

  # Build context is the repo ROOT, not the component directory — both images
  # depend on the local common/ package, which has to be inside the context.
  #
  # BUILD_VERSION is what the running container reports as its version. Without
  # it a stale container presents as a missing feature rather than as a stale
  # container, which has cost real debugging time here more than once.
  docker build -f "$dockerfile" --build-arg "BUILD_VERSION=${TAG}" "${tags[@]}" .

  if [[ $PUSH -eq 0 ]]; then
    echo "${c_ok}built${c_off} ${base}:${TAG} ${c_dim}(and :latest, not pushed)${c_off}"
    echo "${c_dim}this is the stacks' default image, so 'up -d' runs it as-is,${c_off}"
    echo "${c_dim}provided ${env_var} and PULL_POLICY are unset in .env.${c_off}"
    prune_old_tags "$base"
    return
  fi

  docker push "${base}:${TAG}"
  [[ $PUSH_LATEST -eq 1 ]] && docker push "${base}:latest"

  echo "${c_ok}pushed${c_off} ${base}:${TAG} ${c_dim}(and :latest)${c_off}"
  echo "${c_dim}stacks tracking :latest pick this up on their next deploy.${c_off}"
  echo "${c_dim}to PIN this exact build instead, in the stack's .env:${c_off}"
  echo "  ${env_var}=${base}:${TAG}"
  prune_old_tags "$base"
}

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

check_login() {
  # Docker has no "am I logged in" command; the config file is the only signal.
  if ! grep -q "$REGISTRY" ~/.docker/config.json 2>/dev/null; then
    echo "${c_bad}Not logged in to ${REGISTRY}${c_off}"
    echo "Run:  docker login ${REGISTRY}"
    echo "For Forgejo/Gitea, use an access token with package read/write scope"
    echo "as the password."
    echo
    echo "Or build without pushing:  $0 $TARGET --no-push"
    exit 1
  fi
}

[[ $PUSH -eq 1 ]] && check_login

case "$TARGET" in
  agent)   build_and_push spark-dash-agent   agent/Dockerfile   AGENT_IMAGE ;;
  backend) build_and_push spark-dash-backend backend/Dockerfile BACKEND_IMAGE ;;
  both)
    echo "${c_warn}Note:${c_off} both images will be built for $(uname -m). The agent must be"
    echo "arm64 (GX10) and the backend whatever the monitoring VM runs — so 'both'"
    echo "is only correct if one host runs both stacks."
    build_and_push spark-dash-agent   agent/Dockerfile   AGENT_IMAGE
    build_and_push spark-dash-backend backend/Dockerfile BACKEND_IMAGE
    ;;
esac

echo
if [[ $PUSH -eq 0 ]]; then
  echo "Built locally. The image exists only on this host's docker daemon, which"
  echo "is all the stacks need: they default to this name and do not pull."
else
  echo "Nothing pulls images on a schedule, so publishing changes nothing until"
  echo "you deploy. Pin the tag above in the stack's .env, then either commit"
  echo "that (if a deploy tool watches it) or run 'docker compose up -d <service>'."
fi
