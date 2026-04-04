#!/usr/bin/env bash
# setup_wireguard_endpoint_monitor.sh — initialize local config and install a timer

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE_CONFIG="${REPO_ROOT}/config/access/wireguard/endpoint-monitor.example.toml"
LOCAL_CONFIG="${REPO_ROOT}/config/access/wireguard/endpoint-monitor.local.toml"
MONITOR_SCRIPT="${REPO_ROOT}/scripts/check_wireguard_endpoint.py"
UNIT_BASENAME="snowbridge-wireguard-endpoint-monitor"
ON_BOOT_SEC="5m"
ON_UNIT_ACTIVE_SEC="15m"
RUN_AS_USER=""
RUN_AS_GROUP=""
INSTALL_SYSTEMD=0
INIT_LOCAL_CONFIGS=0

usage() {
  cat <<'EOF'
Usage: setup_wireguard_endpoint_monitor.sh [options]

Initialize the local endpoint-monitor config and install a periodic systemd timer.

Options:
  --init-local-configs          Copy endpoint-monitor.example.toml to the ignored
                                endpoint-monitor.local.toml path when missing.
  --install-systemd             Install and enable a systemd service + timer.
  --run-as-user USER            Run the monitor service as USER. Default: repo owner.
  --run-as-group GROUP          Run the monitor service as GROUP. Default: repo owner's group.
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
    --run-as-user)
      RUN_AS_USER="$2"
      shift 2
      ;;
    --run-as-group)
      RUN_AS_GROUP="$2"
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
  exit 0
fi

[[ "${EUID}" -eq 0 ]] || fail "run as root (sudo) to install the systemd timer"
[[ -f "${LOCAL_CONFIG}" ]] || fail "local config not found: ${LOCAL_CONFIG}; run --init-local-configs first"
command -v systemctl >/dev/null 2>&1 || fail "systemctl not found"
command -v python3 >/dev/null 2>&1 || fail "python3 not found"

if [[ -z "${RUN_AS_USER}" ]]; then
  RUN_AS_USER="$(stat -c '%U' "${REPO_ROOT}")"
fi
if [[ -z "${RUN_AS_GROUP}" ]]; then
  RUN_AS_GROUP="$(stat -c '%G' "${REPO_ROOT}")"
fi

SERVICE_PATH="/etc/systemd/system/${UNIT_BASENAME}.service"
TIMER_PATH="/etc/systemd/system/${UNIT_BASENAME}.timer"

cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=Refresh snowbridge WireGuard endpoint artifacts when the WAN IP changes
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${RUN_AS_USER}
Group=${RUN_AS_GROUP}
WorkingDirectory=${REPO_ROOT}
ExecStart=/usr/bin/python3 ${MONITOR_SCRIPT} --config ${LOCAL_CONFIG}
EOF

cat > "${TIMER_PATH}" <<EOF
[Unit]
Description=Periodically check for snowbridge WireGuard endpoint drift

[Timer]
OnBootSec=${ON_BOOT_SEC}
OnUnitActiveSec=${ON_UNIT_ACTIVE_SEC}
Persistent=true
Unit=${UNIT_BASENAME}.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now "${UNIT_BASENAME}.timer"

printf 'installed %s and %s\n' "${SERVICE_PATH}" "${TIMER_PATH}"
printf 'enabled %s.timer\n' "${UNIT_BASENAME}"
