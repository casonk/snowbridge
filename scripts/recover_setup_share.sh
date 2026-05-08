#!/usr/bin/env bash
# recover_setup_share.sh — fsck and remount /mnt/setup after an emergency_ro event
#
# Ext4 remounts the filesystem read-only when it detects an I/O error (e.g. a
# momentary USB disconnect).  This script handles both recovery scenarios:
#
#   1. Simple emergency_ro — LUKS device is still alive, UUID symlink exists.
#      Unmount → e2fsck → remount.
#
#   2. Stale LUKS device — USB drive reconnected with a new device handle but
#      the old dm-3 is still in device mapper, UUID symlink is gone.
#      Close stale dm → reopen LUKS via auto-pass → e2fsck → remount.
#
# The filesystem is addressed by UUID so the script survives device-name
# reassignment (sde → sdf, etc.) after a reconnect.
#
# Usage:
#   sudo bash scripts/recover_setup_share.sh

set -euo pipefail

LUKS_NAME=setup                                 # /dev/mapper/setup = dm-3
LUKS_UUID=e314bac2-d14e-48aa-8956-252530c4522d  # LUKS container on the USB drive
FS_UUID=1e8050bf-248e-42a5-9e1f-53119e429800    # ext4 filesystem inside LUKS
MOUNT_POINT=/mnt/setup
BIND_SOURCE=/mnt/setup/bully/info/receipt
BIND_TARGET=/srv/snowbridge/share/receipt

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "error: run as root (sudo)" >&2
    exit 1
fi

# ── 1. Tear down mounts (best-effort; they may already be gone) ───────────────
echo "==> Unmounting ${BIND_TARGET}…"
if mountpoint -q "${BIND_TARGET}" 2>/dev/null; then umount "${BIND_TARGET}"; fi

echo "==> Unmounting ${MOUNT_POINT}…"
if mountpoint -q "${MOUNT_POINT}" 2>/dev/null; then umount -l "${MOUNT_POINT}"; fi

# ── 2. If the LUKS device is stale, close it and reopen ──────────────────────
DEV="/dev/disk/by-uuid/${FS_UUID}"
if [[ ! -e "${DEV}" ]]; then
    echo "==> UUID symlink missing — LUKS device is stale after USB reconnect."

    if [[ -e "/dev/mapper/${LUKS_NAME}" ]]; then
        echo "==> Closing stale /dev/mapper/${LUKS_NAME}…"
        cryptsetup close "${LUKS_NAME}" 2>/dev/null || true
    fi

    # Find the physical device that holds the LUKS container by its UUID.
    LUKS_DEV="/dev/disk/by-uuid/${LUKS_UUID}"
    if [[ ! -e "${LUKS_DEV}" ]]; then
        echo "error: LUKS container UUID ${LUKS_UUID} not found." >&2
        echo "       Is the USB drive connected and powered?" >&2
        exit 1
    fi

    echo "==> Reopening LUKS volume '${LUKS_NAME}' from $(realpath "${LUKS_DEV}")…"
    # auto-pass must run as the non-root user (editable install, user-owned socket).
    # It must also run from within the snowbridge repo so the daemon resolves the
    # caller identity correctly via /proc/<pid>/cwd.
    REAL_USER="${SUDO_USER:-user}"
    AUTO_PASS="${AUTO_PASS:-/home/${REAL_USER}/.local/bin/auto-pass}"
    AUTO_PASS_SOCK="${AUTO_PASS_SOCK:-/home/${REAL_USER}/.cache/auto-pass/provisioning.sock}"
    SNOWBRIDGE_ROOT="$(cd "$(dirname "$(realpath "$0")")" && cd .. && pwd)"
    PASSPHRASE=$(sudo -u "${REAL_USER}" bash -c \
        "cd '${SNOWBRIDGE_ROOT}' && '${AUTO_PASS}' provision-get machines/luks/luks-floppy-setup --field password --db infra --socket '${AUTO_PASS_SOCK}'") || {
        echo "error: could not retrieve LUKS passphrase — is the auto-pass daemon unlocked?" >&2
        echo "       Run: auto-pass unlock   then re-run this script." >&2
        exit 1
    }
    printf '%s' "${PASSPHRASE}" | cryptsetup open --key-file - "${LUKS_DEV}" "${LUKS_NAME}"
    unset PASSPHRASE

    # Wait for udev to create the by-uuid symlink (usually instant).
    for i in {1..10}; do
        [[ -e "${DEV}" ]] && break
        sleep 1
    done

    if [[ ! -e "${DEV}" ]]; then
        echo "error: UUID symlink still missing after reopening LUKS." >&2
        exit 1
    fi
fi

# ── 3. fsck ───────────────────────────────────────────────────────────────────
echo "==> Running e2fsck on UUID=${FS_UUID}…"
e2fsck -fy "${DEV}"

# ── 4. Remount ────────────────────────────────────────────────────────────────
echo "==> Mounting ${MOUNT_POINT}…"
mount -t ext4 "${DEV}" "${MOUNT_POINT}"

echo "==> Binding ${BIND_SOURCE} → ${BIND_TARGET}…"
mount --bind "${BIND_SOURCE}" "${BIND_TARGET}"

echo "==> Done. ${MOUNT_POINT} is read-write."
