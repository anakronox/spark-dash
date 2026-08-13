#!/usr/bin/env bash
# Push deploy/central and deploy/node to their Dockhand stack repos.
#
#   ./scripts/sync-stack-repos.sh            # sync both
#   ./scripts/sync-stack-repos.sh central    # just one
#   DRY_RUN=1 ./scripts/sync-stack-repos.sh  # show the diff, push nothing
#
# The stack repos are copies, so they can drift from the code they deploy.
# This makes the source repo authoritative and the sync mechanical.
#
# `.env` is DELIBERATELY NOT SYNCED. It holds each deployment's real values
# (node ids, router URLs) and is maintained in the stack repo. Overwriting it
# from .env.example would wipe a live deployment's config.

set -euo pipefail

REGISTRY_HOST="${REGISTRY_HOST:-forgejo.indielab.tech}"
OWNER="${OWNER:-brian}"
DRY_RUN="${DRY_RUN:-}"

c_ok=$'\033[32m'; c_warn=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! git diff-index --quiet HEAD -- 2>/dev/null; then
  echo "${c_warn}Working tree is dirty.${c_off} Syncing uncommitted changes means the"
  echo "stack repo won't correspond to any commit here. Commit first."
  exit 1
fi

SOURCE_SHA="$(git rev-parse --short HEAD)"

sync_one() {
  local dir="$1" repo="$2"
  local src="$REPO_ROOT/deploy/$dir"
  local work
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' RETURN

  echo
  echo "── $dir → $repo ─────────────────────────────"

  git clone -q "https://${REGISTRY_HOST}/${OWNER}/${repo}.git" "$work" 2>/dev/null || {
    echo "could not clone ${repo}; does it exist and are credentials available?"
    return 1
  }

  # Preserve the deployment's own files: .env is live config, and .git is the
  # repo itself. Copied to a file rather than captured in a variable —
  # command substitution strips trailing newlines, which would show up as a
  # spurious .env diff on every single sync.
  local keep_env="$work/../.env.keep"
  [[ -f "$work/.env" ]] && cp "$work/.env" "$keep_env"

  # Mirror the source directory, dropping anything no longer present so a
  # deleted file doesn't linger in the deployed stack.
  find "$work" -mindepth 1 -maxdepth 1 ! -name .git ! -name .env -exec rm -rf {} +
  cp -R "$src"/. "$work"/

  # .env.example is copied (it's documentation); the live .env is restored.
  [[ -f "$keep_env" ]] && cp "$keep_env" "$work/.env" && rm -f "$keep_env"

  cat > "$work/SOURCE.md" <<EOF
# Generated stack — do not edit here

This repository is **deployed by Dockhand** and is a copy of
\`deploy/$dir/\` in the source repo:

  https://${REGISTRY_HOST}/${OWNER}/spark-dash-homegrown

Synced from commit \`${SOURCE_SHA}\`.

Edit there and re-sync with:

    ./scripts/sync-stack-repos.sh

Editing files here directly means the next sync silently overwrites them, and
the compose config no longer matches the code it deploys.

**\`.env\` is the exception** — it holds this deployment's actual values and is
maintained here, not in the source repo.
EOF

  cd "$work"
  if git diff --quiet && git diff --cached --quiet && [[ -z "$(git status --porcelain)" ]]; then
    echo "${c_dim}already up to date${c_off}"
    cd "$REPO_ROOT"
    return 0
  fi

  git add -A
  echo "${c_dim}$(git diff --cached --stat | tail -5)${c_off}"

  if [[ -n "$DRY_RUN" ]]; then
    echo "${c_warn}DRY_RUN${c_off} — not pushing"
    cd "$REPO_ROOT"
    return 0
  fi

  git -c user.name="Brian" -c user.email="nightonthesun@gmail.com" \
      commit -q -m "Sync from spark-dash-homegrown@${SOURCE_SHA}"
  git push -q origin HEAD
  echo "${c_ok}pushed${c_off} ${repo}"
  cd "$REPO_ROOT"
}

case "${1:-both}" in
  central) sync_one central spark-dash-stack-central ;;
  node)    sync_one node spark-dash-stack-node ;;
  both)
    sync_one central spark-dash-stack-central
    sync_one node spark-dash-stack-node
    ;;
  *) echo "usage: $0 {central|node|both}"; exit 2 ;;
esac

echo
echo "Dockhand redeploys on the git change it just saw."
