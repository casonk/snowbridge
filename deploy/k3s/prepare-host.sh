#!/usr/bin/env bash
# prepare-host.sh [--print-env] — check that the pod can use the host folders it mounts. No sudo, changes nothing.
#
# The pod mounts the real source folders of the share (not the bind aliases), the File Browser database and its
# config. Each must exist, and all must carry one SELinux label that a pod can use: type container_file_t and the
# same category pair, which the pod then adopts (securityContext.seLinuxOptions.level). If they differ, this prints
# the exact commands for you to run; undo any of them with `restorecon -RF <path>`.
#   --print-env   after the checks, print POD_UID, POD_GID and SELINUX_LEVEL for apply.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_DIR="${SNOWBRIDGE_FILES_DB_DIR:-/var/lib/snowbridge/filebrowser/database}"

mapfile -t paths < <(python3 -I "$HERE/render_volumes.py" paths)
[ "${#paths[@]}" -gt 0 ] || { echo "no host paths to check" >&2; exit 1; }

levels=() bad=0
for p in "${paths[@]}"; do
  if [ ! -d "$p" ]; then
    echo "missing: $p (is the encrypted volume unlocked and mounted?)" >&2
    bad=1
    continue
  fi
  context="$(stat -c %C "$p")"
  IFS=: read -r _user _role type level_a level_b <<<"$context"
  level="$level_a${level_b:+:$level_b}"
  if [ "$type" != container_file_t ]; then
    echo "label: $p is $type, not container_file_t" >&2
    echo "  fix:  chcon -R -t container_file_t '$p'   (undo: restorecon -RF '$p')" >&2
    bad=1
  fi
  levels+=("$level")
done
[ "$bad" -eq 0 ] || exit 1

unique="$(printf '%s\n' "${levels[@]}" | sort -u)"
if [ "$(printf '%s\n' "$unique" | wc -l)" -ne 1 ]; then
  echo "the folders carry different SELinux categories, so one pod cannot read them all:" >&2
  printf '  %s\n' $unique >&2
  echo "  fix: give them one level, e.g. sudo chcon -R -l s0 <each path above>   (undo: restorecon -RF <path>)" >&2
  exit 1
fi

if [ "${1:-}" = "--print-env" ]; then
  echo "POD_UID=$(stat -c %u "$DB_DIR")"
  echo "POD_GID=$(stat -c %g "$DB_DIR")"
  echo "SELINUX_LEVEL=$unique"
else
  echo "host folders ready: ${#paths[@]} paths, one SELinux level"
fi
