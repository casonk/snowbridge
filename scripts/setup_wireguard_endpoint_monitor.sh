#!/usr/bin/env bash
# setup_wireguard_endpoint_monitor.sh — initialize local config and install a timer

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE_CONFIG="${REPO_ROOT}/config/access/wireguard/endpoint-monitor.example.toml"
LOCAL_CONFIG="${REPO_ROOT}/config/access/wireguard/endpoint-monitor.local.toml"
TEMPLATE_PATH="${REPO_ROOT}/config/clockwork/wireguard-endpoint-monitor.toml.template"
UNIT_BASENAME="snowbridge-wireguard-endpoint-monitor"
ON_BOOT_SEC="5m"
ON_UNIT_ACTIVE_SEC="15m"
RUN_AS_USER=""
RUN_AS_GROUP=""
UNIT_DIR="/etc/systemd/system"
CLOCKWORK_REPO="${CLOCKWORK_REPO:-${REPO_ROOT}/../clockwork}"
INSTALL_SYSTEMD=0
INIT_LOCAL_CONFIGS=0
RENDER_ONLY=0

usage() {
  cat <<'EOF'
Usage: setup_wireguard_endpoint_monitor.sh [options]

Initialize the local endpoint-monitor config and install a periodic systemd timer.

Options:
  --init-local-configs          Copy endpoint-monitor.example.toml to the ignored
                                endpoint-monitor.local.toml path when missing.
  --install-systemd             Install and enable a systemd service + timer.
  --render-only                Render the systemd files only; skip systemctl.
  --run-as-user USER            Run the monitor service as USER. Default: repo owner.
  --run-as-group GROUP          Run the monitor service as GROUP. Default: repo owner's group.
  --unit-dir DIR                Override the target systemd unit directory.
  --clockwork-repo PATH         Override the sibling clockwork repo path fallback.
  --on-boot-sec DURATION        systemd timer OnBootSec. Default: 5m
  --on-unit-active-sec DURATION systemd timer OnUnitActiveSec. Default: 15m
  --help                        Show this help text.

Typical flow:
  ./scripts/setup_wireguard_endpoint_monitor.sh --init-local-configs
  # edit config/access/wireguard/endpoint-monitor.local.toml
  sudo ./scripts/setup_wireguard_endpoint_monitor.sh --install-systemd
EOF
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --init-local-configs)
      INIT_LOCAL_CONFIGS=1
      shift
      ;;
    --install-systemd)
      INSTALL_SYSTEMD=1
      shift
      ;;
    --render-only)
      RENDER_ONLY=1
      shift
      ;;
    --run-as-user)
      RUN_AS_USER="$2"
      shift 2
      ;;
    --run-as-group)
      RUN_AS_GROUP="$2"
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

if (( INIT_LOCAL_CONFIGS == 1 )); then
  if [[ -e "${LOCAL_CONFIG}" ]]; then
    printf 'keep existing %s\n' "${LOCAL_CONFIG}"
  else
    install -D -m 600 "${EXAMPLE_CONFIG}" "${LOCAL_CONFIG}"
    printf 'created %s\n' "${LOCAL_CONFIG}"
  fi
fi

if (( INSTALL_SYSTEMD == 0 )); then
  if (( RENDER_ONLY == 1 )); then
    INSTALL_SYSTEMD=1
  else
    exit 0
  fi
fi

escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

if command -v clockwork >/dev/null 2>&1; then
  CLOCKWORK_CMD=(clockwork)
else
  [[ -d "${CLOCKWORK_REPO}/src/clockwork" ]] || fail "clockwork not found at ${CLOCKWORK_REPO}"
  export PYTHONPATH="${CLOCKWORK_REPO}/src${PYTHONPATH:+:${PYTHONPATH}}"
  CLOCKWORK_CMD=(python3 -m clockwork)
fi

[[ -f "${TEMPLATE_PATH}" ]] || fail "missing template: ${TEMPLATE_PATH}"
[[ -f "${LOCAL_CONFIG}" ]] || fail "local config not found: ${LOCAL_CONFIG}; run --init-local-configs first"
command -v python3 >/dev/null 2>&1 || fail "python3 not found"

if (( RENDER_ONLY == 0 )); then
  [[ "${EUID}" -eq 0 ]] || fail "run as root (sudo) to install the systemd timer"
  command -v systemctl >/dev/null 2>&1 || fail "systemctl not found"
fi

if [[ -z "${RUN_AS_USER}" ]]; then
  RUN_AS_USER="$(stat -c '%U' "${REPO_ROOT}")"
fi
if [[ -z "${RUN_AS_GROUP}" ]]; then
  RUN_AS_GROUP="$(stat -c '%G' "${REPO_ROOT}")"
fi

TMP_MANIFEST="$(mktemp)"
trap 'rm -f "${TMP_MANIFEST}"' EXIT

sed \
  -e "s|__REPO_ROOT__|$(escape_sed_replacement "${REPO_ROOT}")|g" \
  -e "s|__LOCAL_CONFIG__|$(escape_sed_replacement "${LOCAL_CONFIG}")|g" \
  -e "s|__RUN_AS_USER__|$(escape_sed_replacement "${RUN_AS_USER}")|g" \
  -e "s|__RUN_AS_GROUP__|$(escape_sed_replacement "${RUN_AS_GROUP}")|g" \
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
