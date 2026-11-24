#!/usr/bin/env bash
# douglas/fetch-work.sh — bring what a job of Douglas's changed into a
# checkout on this machine.
#
# Run it in a checkout of the repository the job worked on:
#
#     cd ~/code/kettle && ~/code/DougMissionControl/douglas/fetch-work.sh
#     .../fetch-work.sh a0608c        # the job whose branch has that in its name
#     DOUGLAS=you@douglas .../fetch-work.sh
#
# Douglas never pushes. What a job changed is on a branch of its own
# (douglas/<a few words>-<id>) in his clone, and stays there. This fetches
# that branch over ssh, as a git bundle, into a branch of the same name
# here. Nothing is merged and nothing is pushed: read it
# (git log -p <branch> --not --remotes=origin), and do with it what you like.
#
# It needs ssh to Douglas with sudo there (his clones belong to the
# douglas-work user), and this checkout's origin to be the repository on
# GitHub that he cloned.
set -euo pipefail

host="${DOUGLAS:-douglas}"
want="${1:-}"
[[ "$want" =~ ^[A-Za-z0-9-]*$ ]] || { echo "a job is named by letters, digits and hyphens" >&2; exit 1; }
top="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "run this in a checkout of the repository" >&2; exit 1; }
url="$(git -C "$top" remote get-url origin 2>/dev/null)" || { echo "this checkout has no origin" >&2; exit 1; }
repo="${url%/}"; repo="${repo%.git}"
repo="$(printf '%s' "$repo" | sed -E 's#^.*[:/]([^/:]+/[^/]+)$#\1#')"
[[ "$repo" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$ ]] \
    || { echo "cannot tell which GitHub repository this is a checkout of ($url)" >&2; exit 1; }

# His clone of it (GitHub does not mind the case of a name, so neither does
# this), and the newest branch a job left there, or the one asked for.
branch="$(ssh "$host" "sudo -u douglas-work bash -s -- '$repo' '$want'" <<'REMOTE'
set -euo pipefail
cd /        # not the home of whoever logged in: this user may not be in it
bare="$(find /var/lib/douglas-work/repos -mindepth 2 -maxdepth 2 -type d -ipath "*/$1.git" | head -1)"
[ -n "$bare" ] || { echo "Douglas has no clone of $1" >&2; exit 3; }
git -C "$bare" for-each-ref --sort=-committerdate --format='%(refname:short)' refs/heads/douglas/ \
    | { grep -F -- "$2" || true; } | head -1
REMOTE
)" || exit 1
[ -n "$branch" ] || { echo "no job has left a branch${want:+ like $want} in Douglas's clone of $repo" >&2; exit 1; }
[[ "$branch" =~ ^douglas/[a-z0-9-]+$ ]] || { echo "that is no branch of a job's: $branch" >&2; exit 1; }
if git -C "$top" show-ref --verify --quiet "refs/heads/$branch"; then
    echo "$branch is here already. Nothing was fetched."
    exit 0
fi

# Only what GitHub does not have: the job's own commits. They sit on top of a
# commit that is on GitHub, so this checkout needs that one first.
git -C "$top" fetch --quiet origin
bundle="$(mktemp)"
trap 'rm -f "$bundle"' EXIT
ssh "$host" "sudo -u douglas-work bash -s -- '$repo' '$branch'" > "$bundle" <<'REMOTE'
set -euo pipefail
cd /        # not the home of whoever logged in: this user may not be in it
bare="$(find /var/lib/douglas-work/repos -mindepth 2 -maxdepth 2 -type d -ipath "*/$1.git" | head -1)"
git -C "$bare" bundle create --quiet - "refs/heads/$2" --not --remotes=origin
REMOTE
git -C "$top" bundle verify --quiet "$bundle" >/dev/null 2>&1 \
    || { echo "what came from Douglas does not fit this checkout: is it behind GitHub? (git fetch origin)" >&2; exit 1; }
git -C "$top" fetch --quiet "$bundle" "refs/heads/$branch:refs/heads/$branch"

echo "Fetched $branch from Douglas. It is not merged and not pushed."
git -C "$top" log --format='  %h %an, %ad: %s' --date=short "refs/heads/$branch" --not --remotes=origin
first="$(git -C "$top" rev-list --reverse "refs/heads/$branch" --not --remotes=origin | head -1)"
[ -z "$first" ] || git -C "$top" diff --stat "$first^" "refs/heads/$branch" | tail -8
echo "To read it:  git log -p $branch --not --remotes=origin"
