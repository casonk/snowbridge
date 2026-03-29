#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SERVER_EXAMPLE="${REPO_ROOT}/config/access/wireguard/wg0-server.example.conf"
SERVER_LOCAL_DEFAULT="${REPO_ROOT}/config/access/wireguard/wg0-server.local.conf"
IPHONE_EXAMPLE="${REPO_ROOT}/config/access/wireguard/iphone-peer.example.conf"
IPHONE_LOCAL_DEFAULT="${REPO_ROOT}/config/access/wireguard/iphone-peer.local.conf"
SERVER_DEST_DEFAULT="/etc/wireguard/wg0.conf"
IP_FORWARD_SYSCTL="/etc/sysctl.d/99-snowbridge-wireguard.conf"

SERVER_CONFIG="${SERVER_LOCAL_DEFAULT}"
IPHONE_CONFIG="${IPHONE_LOCAL_DEFAULT}"
SERVER_DEST="${SERVER_DEST_DEFAULT}"
INIT_LOCAL_CONFIGS=0
ENABLE_IP_FORWARD=0
SKIP_START=0
PRINT_IPHONE_QR=0
QR_OUTPUT=""

usage() {
  cat <<'EOF'
Usage: setup_wireguard.sh [options]

Set up the snowbridge WireGuard server from local-only config files.

Options:
  --init-local-configs     Copy the example configs to local-only .local files.
  --server-config PATH     Local server config to install.
  --iphone-config PATH     Local iPhone peer config for QR rendering.
  --server-dest PATH       Destination server config path. Default: /etc/wireguard/wg0.conf
  --enable-ip-forward      Write a sysctl drop-in enabling IPv4 and IPv6 forwarding.
  --skip-start             Do not enable or restart wg-quick@wg0 after installing config.
  --print-iphone-qr        Print an ANSI QR code for the iPhone peer config.
  --qr-output PATH         Write a PNG QR code for the iPhone peer config.
  --help                   Show this help text.

This installer will install missing runtime packages automatically when a
supported package manager is available. Currently supported: dnf, apt-get, yum.

Typical flow:
  ./scripts/setup_wireguard.sh --init-local-configs
  # edit config/access/wireguard/*.local.conf
  sudo ./scripts/setup_wireguard.sh --enable-ip-forward --print-iphone-qr
EOF
}

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

find_package_manager() {
  if command -v dnf >/dev/null 2>&1; then
    printf 'dnf\n'
    return
  fi
  if command -v apt-get >/dev/null 2>&1; then
    printf 'apt-get\n'
    return
  fi
  if command -v yum >/dev/null 2>&1; then
    printf 'yum\n'
    return
  fi
  fail "missing runtime packages and no supported package manager found (supported: dnf, apt-get, yum)"
}

install_os_packages() {
  local package_manager
  package_manager="$(find_package_manager)"

  case "${package_manager}" in
    dnf)
      log "install missing packages with dnf: $*"
      dnf install -y "$@"
      ;;
    apt-get)
      log "refresh apt package metadata"
      apt-get update
      log "install missing packages with apt-get: $*"
      DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
      ;;
    yum)
      log "install missing packages with yum: $*"
      yum install -y "$@"
      ;;
    *)
      fail "unsupported package manager: ${package_manager}"
      ;;
  esac
}

install_runtime_packages_if_needed() {
  local missing_packages=()

  if ! command -v wg-quick >/dev/null 2>&1; then
    missing_packages+=(wireguard-tools)
  fi

  if (( PRINT_IPHONE_QR == 1 )) || [[ -n "${QR_OUTPUT}" ]]; then
    if ! command -v qrencode >/dev/null 2>&1; then
      missing_packages+=(qrencode)
    fi
  fi

  if (( ${#missing_packages[@]} == 0 )); then
    return
  fi

  install_os_packages "${missing_packages[@]}"

  command -v wg-quick >/dev/null 2>&1 || fail "wg-quick still missing after package install"
  if (( PRINT_IPHONE_QR == 1 )) || [[ -n "${QR_OUTPUT}" ]]; then
    command -v qrencode >/dev/null 2>&1 || fail "qrencode still missing after package install"
  fi
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || fail "run as root for installation steps"
}

copy_if_missing() {
  local source="$1"
  local target="$2"
  if [[ -e "${target}" ]]; then
    log "keep existing ${target}"
    return
  fi
  install -D -m 600 "${source}" "${target}"
  log "created ${target}"
}

contains_placeholders() {
  grep -Eq '<[^>]+>' "$1"
}

check_local_config() {
  local path="$1"
  local label="$2"
  [[ -f "${path}" ]] || fail "${label} not found: ${path}"
  contains_placeholders "${path}" && fail "${label} still contains placeholder values: ${path}"
}

render_iphone_qr() {
  (( PRINT_IPHONE_QR == 0 )) && [[ -z "${QR_OUTPUT}" ]] && return

  require_command qrencode
  check_local_config "${IPHONE_CONFIG}" "iPhone peer config"

  if (( PRINT_IPHONE_QR == 1 )); then
    log "ANSI QR for ${IPHONE_CONFIG}:"
    qrencode -t ANSIUTF8 < "${IPHONE_CONFIG}"
  fi

  if [[ -n "${QR_OUTPUT}" ]]; then
    install -d "$(dirname "${QR_OUTPUT}")"
    qrencode -o "${QR_OUTPUT}" < "${IPHONE_CONFIG}"
    log "wrote ${QR_OUTPUT}"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --init-local-configs)
      INIT_LOCAL_CONFIGS=1
      shift
      ;;
    --server-config)
      SERVER_CONFIG="$2"
      shift 2
      ;;
    --iphone-config)
      IPHONE_CONFIG="$2"
      shift 2
      ;;
    --server-dest)
      SERVER_DEST="$2"
      shift 2
      ;;
    --enable-ip-forward)
      ENABLE_IP_FORWARD=1
      shift
      ;;
    --skip-start)
      SKIP_START=1
      shift
      ;;
    --print-iphone-qr)
      PRINT_IPHONE_QR=1
      shift
      ;;
    --qr-output)
      QR_OUTPUT="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

if (( INIT_LOCAL_CONFIGS == 1 )); then
  copy_if_missing "${SERVER_EXAMPLE}" "${SERVER_CONFIG}"
  copy_if_missing "${IPHONE_EXAMPLE}" "${IPHONE_CONFIG}"
  log "edit the local configs, then rerun with sudo to install them"
  exit 0
fi

require_root
require_command install
require_command systemctl
install_runtime_packages_if_needed

check_local_config "${SERVER_CONFIG}" "server config"
INTERFACE_NAME="$(basename "${SERVER_DEST}" .conf)"

install -d /etc/wireguard
install -m 600 "${SERVER_CONFIG}" "${SERVER_DEST}"
log "installed ${SERVER_DEST}"

if (( ENABLE_IP_FORWARD == 1 )); then
  printf 'net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n' > "${IP_FORWARD_SYSCTL}"
  sysctl -p "${IP_FORWARD_SYSCTL}" >/dev/null
  log "updated ${IP_FORWARD_SYSCTL}"
fi

if (( SKIP_START == 0 )); then
  systemctl enable --now "wg-quick@${INTERFACE_NAME}.service"
  systemctl restart "wg-quick@${INTERFACE_NAME}.service"
  log "enabled and restarted wg-quick@${INTERFACE_NAME}.service"
fi

render_iphone_qr

log "next checks:"
log "  sudo wg show ${INTERFACE_NAME}"
log "  sudo systemctl status wg-quick@${INTERFACE_NAME}.service"
