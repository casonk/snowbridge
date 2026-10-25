#!/usr/bin/env bash
# setup_crew_chief_relay.sh - give File Browser a socket to the local crew-chief Ollama

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/config/web/filebrowser/filebrowser.env.local"
UNIT_BASENAME="snowbridge-crew-chief-relay"
UNIT_DIR="/etc/systemd/system"
SOCKET_NAME="crew-chief.sock"
OLLAMA_PORT="11434"
DRY_RUN=0
UNINSTALL=0

usage() {
  cat <<'EOF'
Usage: setup_crew_chief_relay.sh [options]

Install a systemd service that relays a unix socket to the host-local
crew-chief Ollama service, so the File Browser container can reach Ollama and
nothing else on host loopback. The socket is created as
${FILEBROWSER_CONFIG_DIR}/crew-chief.sock (seen as /config/crew-chief.sock in
the container) by a service running as SNOWBRIDGE_UID:SNOWBRIDGE_GID, so
only that user can connect to it.

The service, not a .socket unit, creates the socket: the compose `:Z` mount
labels the config directory container_file_t, where SELinux denies PID 1
socket creation.

Options:
  --env-file PATH     File Browser env file. Default: config/web/filebrowser/filebrowser.env.local
  --ollama-port PORT  crew-chief Ollama port on 127.0.0.1. Default: 11434
  --unit-dir DIR      Override the target systemd unit directory.
  --dry-run           Print the rendered unit without installing it.
  --uninstall         Stop, disable, and remove the relay unit.
  --help              Show this help text.

Typical flow:
  sudo ./scripts/setup_crew_chief_relay.sh
EOF
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

# Read KEY=value from the env file without sourcing it.
env_value() {
  local key="$1" line value=""
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ "${line}" =~ ^[[:space:]]*${key}=(.*)$ ]] || continue
    value="${BASH_REMATCH[1]}"
  done <"${ENV_FILE}"
  value="${value%\"}"
  value="${value#\"}"
  printf '%s' "${value}"
}

find_tool() {
  local candidate
  for candidate in "$@"; do
    if [[ -x "${candidate}" ]]; then
      printf '%s' "${candidate}"
      return 0
    fi
  done
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="$2"
      shift 2
      ;;
    --ollama-port)
      OLLAMA_PORT="$2"
      shift 2
      ;;
    --unit-dir)
      UNIT_DIR="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --uninstall)
      UNINSTALL=1
      shift
      ;;
    --help | -h)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

SERVICE_UNIT="${UNIT_DIR}/${UNIT_BASENAME}.service"
# Earlier versions of this script installed a .socket unit; clean it up.
LEGACY_SOCKET_UNIT="${UNIT_DIR}/${UNIT_BASENAME}.socket"

if ((UNINSTALL)); then
  if ((DRY_RUN)); then
    printf 'would disable %s.service and remove %s\n' "${UNIT_BASENAME}" "${SERVICE_UNIT}"
    exit 0
  fi
  systemctl disable --now "${UNIT_BASENAME}.socket" "${UNIT_BASENAME}.service" 2>/dev/null || true
  rm -f "${LEGACY_SOCKET_UNIT}" "${SERVICE_UNIT}"
  systemctl daemon-reload
  printf 'removed %s relay unit\n' "${UNIT_BASENAME}"
  exit 0
fi

[[ "${OLLAMA_PORT}" =~ ^[0-9]+$ ]] || fail "--ollama-port must be numeric"
[[ -r "${ENV_FILE}" ]] || fail "cannot read env file: ${ENV_FILE}"

RUN_UID="$(env_value SNOWBRIDGE_UID)"
RUN_GID="$(env_value SNOWBRIDGE_GID)"
CONFIG_DIR="$(env_value FILEBROWSER_CONFIG_DIR)"
[[ "${RUN_UID}" =~ ^[0-9]+$ ]] || fail "SNOWBRIDGE_UID missing or not numeric in ${ENV_FILE}"
[[ "${RUN_GID}" =~ ^[0-9]+$ ]] || fail "SNOWBRIDGE_GID missing or not numeric in ${ENV_FILE}"
[[ "${CONFIG_DIR}" == /* ]] || fail "FILEBROWSER_CONFIG_DIR must be an absolute path in ${ENV_FILE}"
SOCKET_PATH="${CONFIG_DIR%/}/${SOCKET_NAME}"
# sun_path is 108 bytes including the terminator.
((${#SOCKET_PATH} < 108)) || fail "socket path is too long for a unix socket: ${SOCKET_PATH}"

PROXYD="$(find_tool /usr/lib/systemd/systemd-socket-proxyd /lib/systemd/systemd-socket-proxyd)" ||
  ((DRY_RUN)) || fail "systemd-socket-proxyd not found"
ACTIVATE="$(find_tool /usr/bin/systemd-socket-activate /bin/systemd-socket-activate)" ||
  ((DRY_RUN)) || fail "systemd-socket-activate not found"
PROXYD="${PROXYD:-/usr/lib/systemd/systemd-socket-proxyd}"
ACTIVATE="${ACTIVATE:-/usr/bin/systemd-socket-activate}"

SERVICE_CONTENT="[Unit]
Description=Snowbridge File Browser relay to the local crew-chief Ollama
After=network.target

[Service]
User=${RUN_UID}
Group=${RUN_GID}
UMask=0077
ExecStartPre=/usr/bin/rm -f ${SOCKET_PATH}
ExecStart=${ACTIVATE} --listen=${SOCKET_PATH} ${PROXYD} 127.0.0.1:${OLLAMA_PORT}
ExecStopPost=/usr/bin/rm -f ${SOCKET_PATH}
Restart=on-failure
RestartSec=5s
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
IPAddressDeny=any
IPAddressAllow=localhost

[Install]
WantedBy=multi-user.target
"

if ((DRY_RUN)); then
  printf '# %s\n%s' "${SERVICE_UNIT}" "${SERVICE_CONTENT}"
  exit 0
fi

[[ -d "${CONFIG_DIR}" ]] || fail "FILEBROWSER_CONFIG_DIR does not exist: ${CONFIG_DIR}"
if [[ -e "${LEGACY_SOCKET_UNIT}" ]]; then
  systemctl disable --now "${UNIT_BASENAME}.socket" 2>/dev/null || true
  rm -f "${LEGACY_SOCKET_UNIT}"
fi
install -d -m 0755 "${UNIT_DIR}"
printf '%s' "${SERVICE_CONTENT}" >"${SERVICE_UNIT}"
chmod 0644 "${SERVICE_UNIT}"
systemctl daemon-reload
systemctl enable "${UNIT_BASENAME}.service"
systemctl restart "${UNIT_BASENAME}.service"
for _ in 1 2 3 4 5; do
  [[ -S "${SOCKET_PATH}" ]] && break
  sleep 1
done
[[ -S "${SOCKET_PATH}" ]] || fail "relay did not create ${SOCKET_PATH}; see: journalctl -u ${UNIT_BASENAME}.service"
printf 'relay listening on %s -> 127.0.0.1:%s\n' "${SOCKET_PATH}" "${OLLAMA_PORT}"
