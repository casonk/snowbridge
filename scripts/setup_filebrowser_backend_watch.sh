#!/usr/bin/env bash
# setup_filebrowser_backend_watch.sh - install the File Browser backend watchdog timer

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE_PATH="${REPO_ROOT}/config/clockwork/filebrowser-backend-watch.toml.template"
UNIT_BASENAME="snowbridge-filebrowser-backend-watch"
ENV_FILE="${REPO_ROOT}/config/web/filebrowser/filebrowser.env.local"
COMPOSE_FILE="${REPO_ROOT}/config/web/filebrowser/docker-compose.local.yml"
if [[ ! -f "${COMPOSE_FILE}" ]]; then
  COMPOSE_FILE="${REPO_ROOT}/config/web/filebrowser/docker-compose.example.yml"
fi
ON_BOOT_SEC="3m"
ON_UNIT_ACTIVE_SEC="5m"
UNIT_DIR="/etc/systemd/system"
CLOCKWORK_REPO="${CLOCKWORK_REPO:-${REPO_ROOT}/../clockwork}"
INSTALL_SYSTEMD=0
RENDER_ONLY=0

usage() {
  cat <<'EOF'
Usage: setup_filebrowser_backend_watch.sh [options]

Install a systemd timer that checks the local File Browser backend and starts
the compose service when the backend is unavailable.

Options:
  --install-systemd             Install and enable the systemd service + timer.
  --render-only                 Render the systemd files only; skip systemctl.
  --env-file PATH               Local File Browser env file. Default: config/web/filebrowser/filebrowser.env.local
  --compose-file PATH           Compose file. Default: docker-compose.local.yml when present, otherwise example.
  --unit-dir DIR                Override the target systemd unit directory.
  --clockwork-repo PATH         Override the sibling clockwork repo path fallback.
  --on-boot-sec DURATION        systemd timer OnBootSec. Default: 3m
  --on-unit-active-sec DURATION systemd timer OnUnitActiveSec. Default: 5m
  --help                        Show this help text.

Typical flow:
  sudo ./scripts/setup_filebrowser_backend_watch.sh --install-systemd
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
    --env-file)
      ENV_FILE="$2"
      shift 2
      ;;
    --compose-file)
      COMPOSE_FILE="$2"
      shift 2
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
[[ -f "${ENV_FILE}" ]] || fail "env file not found: ${ENV_FILE}"
[[ -f "${COMPOSE_FILE}" ]] || fail "compose file not found: ${COMPOSE_FILE}"
command -v python3 >/dev/null 2>&1 || fail "python3 not found"

if (( RENDER_ONLY == 0 )); then
  [[ "${EUID}" -eq 0 ]] || fail "run as root (sudo) to install the systemd timer"
  command -v systemctl >/dev/null 2>&1 || fail "systemctl not found"
fi

TMP_MANIFEST="$(mktemp)"
trap 'rm -f "${TMP_MANIFEST}"' EXIT

sed \
  -e "s|__REPO_ROOT__|$(escape_sed_replacement "${REPO_ROOT}")|g" \
  -e "s|__ENV_FILE__|$(escape_sed_replacement "${ENV_FILE}")|g" \
  -e "s|__COMPOSE_FILE__|$(escape_sed_replacement "${COMPOSE_FILE}")|g" \
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
