#!/usr/bin/env bash
# Push the spark-dash images to a container registry. MAINTAINER PATH.
#
#   ./scripts/publish-images.sh agent      # run this ON a GX10 (arm64)
#   ./scripts/publish-images.sh backend    # run this ON the host that runs it
#   ./scripts/publish-images.sh --help
#
# MOST INSTALLS NEVER RUN THIS. Building is the whole job for an end user, and
# that is build-images.sh — no registry, no `docker login`, no account
# anywhere. This script exists for whoever publishes the images other people
# pull, which is normally one person.
#
# It BUILDS BY DELEGATING to build-images.sh rather than repeating it, and asks
# that script for the tag rather than deriving its own: two scripts computing
# "the same" tag independently is how you publish an image that is not the one
# you just built.

set -euo pipefail

c_ok=$'\033[32m'; c_bad=$'\033[31m'; c_warn=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${REPO_ROOT}/scripts/build-images.sh"
cd "$REPO_ROOT"

usage() {
  cat <<'EOF'
usage: publish-images.sh {agent|backend|both} [options]

  agent      build on a GX10 (arm64), then push
  backend    build on the host that will run it, then push
  both       both here — only correct if one host runs both

options:
  --tag TAG        override the tag (default: short git sha)
  --no-latest      push only the tag, not :latest
  --allow-arch-change  push even though the tag already holds another
                       architecture (see the warning it prints)
  -h, --help       this

environment:
  REGISTRY   registry host   (default: derived from git remote origin)
  OWNER      registry owner  (default: derived from git remote origin)

Deriving both from the clone's own remote means a fork publishes to its own
registry with no configuration, and no one's personal registry is baked into
a tracked file. Override either when the registry is not where the source
lives.

To build WITHOUT publishing — which is what most installs want — use
build-images.sh instead.
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

TARGET=""; PUSH_LATEST=1; TAG_OVERRIDE=""; ALLOW_ARCH_CHANGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    agent|backend|both)   TARGET="$1" ;;
    --no-latest)          PUSH_LATEST=0 ;;
    --tag)                TAG_OVERRIDE="${2:-}"; shift ;;
    --allow-arch-change)  ALLOW_ARCH_CHANGE=1 ;;
    -h|--help)            usage; exit 0 ;;
    *) echo "${c_bad}unknown argument:${c_off} $1"; echo; usage; exit 2 ;;
  esac
  shift
done

if [[ -z "$TARGET" ]]; then usage; exit 2; fi

if [[ -z "$REGISTRY" || -z "$OWNER" ]]; then
  echo "${c_bad}Cannot work out where to push.${c_off}"
  echo "No usable 'origin' remote, so REGISTRY and OWNER must be set:"
  echo
  echo "  REGISTRY=registry.example.com OWNER=you $0 $TARGET"
  echo
  echo "Or just build, without a registry:  ./scripts/build-images.sh $TARGET"
  exit 2
fi

check_login() {
  # Docker has no "am I logged in" command; the config file is the only signal.
  if ! grep -q "$REGISTRY" ~/.docker/config.json 2>/dev/null; then
    echo "${c_bad}Not logged in to ${REGISTRY}${c_off}"
    echo "Run:  docker login ${REGISTRY}"
    echo "For Forgejo/Gitea, use an access token with package read/write scope"
    echo "as the password."
    echo
    echo "Or just build, without a registry:  ./scripts/build-images.sh $TARGET"
    exit 1
  fi
}
check_login

# ARCHITECTURE IS PART OF A TAG'S IDENTITY, and nothing else here enforces it.
#
# These images are built NATIVELY where they will run, so `:latest` holds
# whichever architecture was last pushed. Publish an amd64 backend over an
# arm64 one and every arm64 puller gets `exec format error` at container start
# — long after they followed the instructions, with a message that names
# nothing useful.
#
# That is not hypothetical for a published repo: a maintainer on an amd64
# monitoring VM and a single-host user on a GB10 want the same tag to mean two
# different things. Until these are proper multi-arch manifest lists, the honest
# behaviour is to refuse and say so.
arch_guard() {
  local ref="$1" local_arch remote_arch
  local_arch="$(docker image inspect "$ref" --format '{{.Os}}/{{.Architecture}}' 2>/dev/null || true)"
  # --verbose IS REQUIRED, and getting this wrong makes the guard useless
  # rather than noisy: a plain `docker manifest inspect` of a single-arch image
  # returns schemaVersion/config/layers and NAMES NO ARCHITECTURE AT ALL, so
  # the check silently found nothing to compare and passed everything. Only a
  # manifest LIST carries platform data without --verbose, and these images are
  # not lists. Verified against the live registry.
  remote_arch="$(docker manifest inspect --verbose "$ref" 2>/dev/null \
    | grep -oE '"architecture": *"[a-z0-9_]+"' | head -1 \
    | sed 's/.*"\([a-z0-9_]*\)"$/\1/' || true)"

  # A tag that does not exist yet, or a manifest we cannot read, is not a
  # conflict — say nothing rather than blocking a first publish.
  [[ -z "$remote_arch" || -z "$local_arch" ]] && return 0
  [[ "$local_arch" == */"$remote_arch" ]] && return 0

  echo
  echo "${c_bad}REFUSING:${c_off} ${ref} currently holds ${remote_arch};"
  echo "this build is ${local_arch}. Overwriting it would break every puller on"
  echo "${remote_arch} with 'exec format error' at container start."
  echo
  echo "  - publishing for a different architecture? use a distinct tag:"
  echo "      $0 $TARGET --tag ${TAG}-${local_arch##*/}"
  echo "  - genuinely replacing the published architecture?"
  echo "      $0 $TARGET --allow-arch-change"
  echo
  [[ $ALLOW_ARCH_CHANGE -eq 1 ]] || return 1
  echo "${c_warn}--allow-arch-change given; continuing.${c_off}"
  return 0
}

# One source of truth for the tag: ask the builder rather than re-deriving it.
TAG="$("$BUILD" --print-tag ${TAG_OVERRIDE:+--tag "$TAG_OVERRIDE"})"

push_one() {
  local name="$1"
  local base="${REGISTRY}/${OWNER}/${name}"

  docker tag "${name}:${TAG}" "${base}:${TAG}"
  arch_guard "${base}:${TAG}" || exit 1
  docker push "${base}:${TAG}"

  if [[ $PUSH_LATEST -eq 1 ]]; then
    docker tag "${name}:${TAG}" "${base}:latest"
    arch_guard "${base}:latest" || exit 1
    docker push "${base}:latest"
  fi

  local env_var="AGENT_IMAGE"
  [[ "$name" == "spark-dash-backend" ]] && env_var="BACKEND_IMAGE"
  echo "${c_ok}pushed${c_off} ${base}:${TAG} ${c_dim}$([[ $PUSH_LATEST -eq 1 ]] && echo '(and :latest)')${c_off}"
  echo "${c_dim}stacks tracking :latest pick this up on their next deploy.${c_off}"
  echo "${c_dim}to PIN this exact build instead, in the stack's .env:${c_off}"
  echo "  ${env_var}=${base}:${TAG}"
}

# Build first, through the one script that knows how.
"$BUILD" "$TARGET" --tag "$TAG"

case "$TARGET" in
  agent)   push_one spark-dash-agent ;;
  backend) push_one spark-dash-backend ;;
  both)    push_one spark-dash-agent; push_one spark-dash-backend ;;
esac

echo
echo "Nothing pulls images on a schedule, so publishing changes nothing until"
echo "you deploy. Pin the tag above in the stack's .env, then either commit"
echo "that (if a deploy tool watches it) or run 'docker compose up -d <service>'."
