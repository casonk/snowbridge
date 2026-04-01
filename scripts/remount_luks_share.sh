#!/usr/bin/env bash
# remount_luks_share.sh — refresh fstab bind mounts after LUKS drives are unlocked
#
# At boot the fstab bind mounts for folders whose sources live under /mnt/ run
# before LUKS is unlocked.  Those mounts therefore capture empty btrfs stubs
# rather than the actual ext4 content.  Run this script once per session, after
# your LUKS volumes are mounted, to unmount the stale binds and re-bind from
# the now-live ext4 paths.
#
# Usage:
#   sudo bash scripts/remount_luks_share.sh
#
# The list of target paths is read directly from the snowbridge fstab block so
# there is no separate config to keep in sync.

set -euo pipefail

SHARE_ROOT=/srv/snowbridge/share

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "error: run as root (sudo)" >&2
    exit 1
fi

# Collect share subdirs whose fstab source is on a /mnt/ path (i.e. a LUKS
# drive). keepass and any future btrfs-native folders are skipped because they
# do not need a post-LUKS remount.
mapfile -t LUKS_TARGETS < <(
    awk '/^# --- snowbridge bind mounts/,/^# --- snowbridge bind mounts.*end/' \
        /etc/fstab \
    | awk -v share="${SHARE_ROOT}" '!/^#/ && index($2, share) == 1 && $1 ~ /^\/mnt\// {print $2}'
)

if [[ ${#LUKS_TARGETS[@]} -eq 0 ]]; then
    echo "No LUKS bind-mount targets found in /etc/fstab under ${SHARE_ROOT}."
    exit 0
fi

echo "Refreshing ${#LUKS_TARGETS[@]} LUKS bind mount(s)…"
for target in "${LUKS_TARGETS[@]}"; do
    # Determine the fstab source for this target.
    src=$(awk -v t="$target" '!/^#/ && $2 == t {print $1; exit}' /etc/fstab)
    if [[ -z "$src" ]]; then
        echo "  warning: no fstab entry found for ${target}, skipping" >&2
        continue
    fi

    # Verify the source is actually accessible (i.e. LUKS is unlocked).
    if [[ ! -d "$src" ]]; then
        echo "  warning: source ${src} does not exist; LUKS may not be unlocked" >&2
        echo "           skipping ${target}" >&2
        continue
    fi

    echo "  ${src} → ${target}"
    if mountpoint -q "$target"; then
        umount "$target"
    fi
    mount "$target"
done

echo "Done. File Browser will reflect the updated share on next directory listing."
