#!/usr/bin/env bash
# setup_share_bind_mount_watch.sh - install the Snowbridge bind-mount watchdog timer

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE_PATH="${REPO_ROOT}/config/clockwork/share-bind-mount-watch.toml.template"
UNIT_BASENAME="snowbridge-share-bind-mount-watch"
UNIT_DIR="/etc/systemd/system"
CLOCKWORK_REPO="${CLOCKWORK_REPO:-${REPO_ROOT}/../clockwork}"
ON_BOOT_SEC="3m"
ON_UNIT_ACTIVE_SEC="5m"
INSTALL_SYSTEMD=0
RENDER_ONLY=0

usage() {
  cat <<'EOF'
Usage: setup_share_bind_mount_watch.sh [options]

Install a systemd timer that checks Snowbridge bind mounts and remounts any
managed /etc/fstab targets that are missing or stale after source volumes are
unlocked.

Options:
  --install-systemd             Install and enable the systemd service + timer.
  --render-only                 Render the systemd files only; skip systemctl.
  --unit-dir DIR                Override the target systemd unit directory.
  --clockwork-repo PATH         Override the sibling clockwork repo path fallback.
  --on-boot-sec DURATION        systemd timer OnBootSec. Default: 3m
  --on-unit-active-sec DURATION systemd timer OnUnitActiveSec. Default: 5m
  --help                        Show this help text.

Typical flow:
  sudo ./scripts/setup_share_bind_mount_watch.sh --install-systemd
EOF
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-systemd)
      INSTALL_SYSTEMD=1
      shift
      ;;
    --render-only)
      RENDER_ONLY=1
      shift
      ;;
    --unit-dir)
      UNIT_DIR="$2"
      shift 2
      ;;
    --clockwork-repo)
      CLOCKWORK_REPO="$2"
      shift 2
      ;;
    --on-boot-sec)
      ON_BOOT_SEC="$2"
      shift 2
      ;;
    --on-unit-active-sec)
      ON_UNIT_ACTIVE_SEC="$2"
      shift 2
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

if (( INSTALL_SYSTEMD == 0 )); then
  if (( RENDER_ONLY == 1 )); then
    INSTALL_SYSTEMD=1
  else
    usage
    exit 0
  fi
fi

if command -v clockwork >/dev/null 2>&1; then
  CLOCKWORK_CMD=(clockwork)
else
  [[ -d "${CLOCKWORK_REPO}/src/clockwork" ]] || fail "clockwork not found at ${CLOCKWORK_REPO}"
  export PYTHONPATH="${CLOCKWORK_REPO}/src${PYTHONPATH:+:${PYTHONPATH}}"
  CLOCKWORK_CMD=(python3 -m clockwork)
fi

[[ -f "${TEMPLATE_PATH}" ]] || fail "missing template: ${TEMPLATE_PATH}"
command -v python3 >/dev/null 2>&1 || fail "python3 not found"

if (( RENDER_ONLY == 0 )); then
  [[ "${EUID}" -eq 0 ]] || fail "run as root (sudo) to install the systemd timer"
  command -v systemctl >/dev/null 2>&1 || fail "systemctl not found"
fi

TMP_MANIFEST="$(mktemp)"
trap 'rm -f "${TMP_MANIFEST}"' EXIT

sed \
  -e "s|__REPO_ROOT__|$(escape_sed_replacement "${REPO_ROOT}")|g" \
  -e "s|__ON_BOOT_SEC__|$(escape_sed_replacement "${ON_BOOT_SEC}")|g" \
  -e "s|__ON_UNIT_ACTIVE_SEC__|$(escape_sed_replacement "${ON_UNIT_ACTIVE_SEC}")|g" \
  "${TEMPLATE_PATH}" > "${TMP_MANIFEST}"

"${CLOCKWORK_CMD[@]}" install \
  --manifest "${TMP_MANIFEST}" \
  --target systemd-system \
  --unit-dir "${UNIT_DIR}"

if (( RENDER_ONLY == 1 )); then
  exit 0
fi

systemctl daemon-reload
systemctl enable --now "${UNIT_BASENAME}.timer"

printf 'enabled %s.timer from %s\n' "${UNIT_BASENAME}" "${UNIT_DIR}"
