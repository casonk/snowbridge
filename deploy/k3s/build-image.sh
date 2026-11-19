#!/usr/bin/env bash
# build-image.sh — build the File Browser fork image and push it to the in-cluster registry (no sudo).
# Needs crew-chief's deploy/k3s/install-registry.sh once, and enable-registry-mirror.sh once as root.
#
# The image is the fork's own Dockerfile plus its binary, built by scripts/build_filebrowser_fork_image.sh (the
# same recipe the host image uses). There is no separate Containerfile to drift from it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
FORK="${SNOWBRIDGE_FILES_FORK_DIR:-$REPO/vendor/filebrowser-upstream}"
CREW_CHIEF="${CREW_CHIEF_DIR:-}"
if [ -z "$CREW_CHIEF" ]; then
  for candidate in "$REPO/../crew-chief" "$REPO/../../util-repos/crew-chief"; do
    [ -x "$candidate/deploy/k3s/push-image.sh" ] && { CREW_CHIEF="$candidate"; break; }
  done
fi
[ -x "$CREW_CHIEF/deploy/k3s/push-image.sh" ] || { echo "cannot find crew-chief's push-image.sh; set CREW_CHIEF_DIR" >&2; exit 1; }
IMAGE="localhost/snowbridge-files:k3s"

# The pod runs what is committed; the host runs whatever is in the checkout. The fork is its own git repository
# (snowbridge ignores vendor/), so the guard looks there. Build only from committed code, or say so explicitly.
[ -d "$FORK/.git" ] || { echo "$FORK is not the File Browser fork checkout; set SNOWBRIDGE_FILES_FORK_DIR" >&2; exit 1; }
if [ -z "${ALLOW_DIRTY:-}" ] && [ -n "$(git -C "$FORK" status --porcelain)" ]; then
  echo "$FORK has uncommitted changes; the image would not match what is committed. Commit them, or set ALLOW_DIRTY=1." >&2
  exit 1
fi

"$REPO/scripts/build_filebrowser_fork_image.sh" --workspace "$FORK" --image "$IMAGE"
"$CREW_CHIEF/deploy/k3s/push-image.sh" "$IMAGE" snowbridge-files:k3s
echo "Next: deploy/k3s/apply.sh"
