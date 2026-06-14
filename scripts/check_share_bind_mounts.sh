#!/usr/bin/env bash
# check_share_bind_mounts.sh - verify and optionally repair snowbridge bind mounts

set -euo pipefail

SHARE_ROOT="/srv/snowbridge/share"
FSTAB_PATH="/etc/fstab"
REPAIR=0
QUIET=0

usage() {
  cat <<'EOF'
Usage: check_share_bind_mounts.sh [options]

Verify bind mounts from the snowbridge managed /etc/fstab block. With
--repair, remount missing or stale targets whose sources are currently
available.

Options:
  --repair           Mount missing targets and remount stale targets.
  --share-root PATH  Share root to check. Default: /srv/snowbridge/share
  --fstab PATH       fstab path to read. Default: /etc/fstab
  --quiet            Only print failures.
  --help             Show this help text.
EOF
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

log() {
  if (( QUIET == 0 )); then
    printf '%s\n' "$*"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repair)
      REPAIR=1
      shift
      ;;
    --share-root)
      SHARE_ROOT="$2"
      shift 2
      ;;
    --fstab)
      FSTAB_PATH="$2"
      shift 2
      ;;
    --quiet)
      QUIET=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

[[ -r "${FSTAB_PATH}" ]] || fail "cannot read ${FSTAB_PATH}"
if (( REPAIR == 1 )) && [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  fail "run as root (sudo) when using --repair"
fi

# Check target existence without requiring traverse permission on parent dirs.
# The share root is mode 2770 (snowbridge:snowbridge), so [[ -d target ]] fails
# for users outside that group.  findmnt queries the kernel mount table instead.
_target_exists() {
  [[ -d "$1" ]] || findmnt --noheadings --target "$1" >/dev/null 2>&1
}

mapfile -t ENTRIES < <(
  awk -v share="${SHARE_ROOT}" '
    /^# --- snowbridge bind mounts: managed block start ---/ { in_block=1; next }
    /^# --- snowbridge bind mounts: managed block end ---/ { in_block=0; next }
    in_block && $0 !~ /^[[:space:]]*#/ && NF >= 2 && index($2, share) == 1 {
      print $1 "\t" $2
    }
  ' "${FSTAB_PATH}"
)

if [[ ${#ENTRIES[@]} -eq 0 ]]; then
  log "No snowbridge bind-mount entries found in ${FSTAB_PATH}."
  exit 0
fi

status=0
repair_count=0

repair_target() {
  local source="$1"
  local target="$2"

  if [[ ! -d "${source}" ]]; then
    printf 'unavailable: %s -> %s (source does not exist)\n' "${source}" "${target}" >&2
    return 1
  fi
  if ! _target_exists "${target}"; then
    printf 'unavailable: %s -> %s (target does not exist)\n' "${source}" "${target}" >&2
    return 1
  fi

  if (( REPAIR == 0 )); then
    return 1
  fi

  if mountpoint -q "${target}"; then
    umount "${target}"
  fi
  mount "${target}"
  source_id="$(stat -Lc '%d:%i' "${source}" 2>/dev/null || true)"
  target_id="$(stat -Lc '%d:%i' "${target}" 2>/dev/null || true)"
  if [[ -z "${source_id}" || "${source_id}" != "${target_id}" ]]; then
    printf 'repair failed: %s -> %s\n' "${source}" "${target}" >&2
    return 1
  fi
  repair_count=$((repair_count + 1))
  log "repaired: ${source} -> ${target}"
  return 0
}

for entry in "${ENTRIES[@]}"; do
  IFS=$'\t' read -r source target <<<"${entry}"

  if [[ ! -d "${source}" ]] || ! _target_exists "${target}"; then
    if ! repair_target "${source}" "${target}"; then
      status=1
    fi
    continue
  fi

  source_id="$(stat -Lc '%d:%i' "${source}" 2>/dev/null || true)"
  target_id="$(stat -Lc '%d:%i' "${target}" 2>/dev/null || true)"

  if [[ -z "${source_id}" || -z "${target_id}" ]]; then
    printf 'unreadable: %s -> %s\n' "${source}" "${target}" >&2
    status=1
    continue
  fi

  if [[ "${source_id}" == "${target_id}" ]]; then
    log "ok: ${source} -> ${target}"
    continue
  fi

  if mountpoint -q "${target}"; then
    printf 'stale: %s -> %s\n' "${source}" "${target}" >&2
  else
    printf 'missing: %s -> %s\n' "${source}" "${target}" >&2
  fi
  if ! repair_target "${source}" "${target}"; then
    status=1
  fi
done

if (( status == 0 )); then
  log "All snowbridge bind mounts are current."
elif (( REPAIR == 1 && repair_count > 0 )); then
  log "Repaired ${repair_count} snowbridge bind mount(s)."
fi

exit "${status}"
