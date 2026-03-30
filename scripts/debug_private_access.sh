#!/usr/bin/env bash
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
DEFAULT_REPORT_DIR="${REPO_ROOT}/reports"
DEFAULT_REPORT_PATH="${DEFAULT_REPORT_DIR}/private-access-debug-${TIMESTAMP}.log"

REPORT_PATH="${DEFAULT_REPORT_PATH}"
WG_INTERFACE="wg0"
PRIVATE_HOSTNAME="files.snowbridge.internal"
PRIVATE_IP="10.99.0.1"
COMPOSE_FILE="${REPO_ROOT}/config/web/filebrowser/docker-compose.example.yml"
ENV_FILE="${REPO_ROOT}/config/web/filebrowser/filebrowser.env.local"
PRIVATE_CADDYFILE="${REPO_ROOT}/config/web/caddy/Caddyfile.private-vpn.local"

usage() {
  cat <<'EOF'
Usage: debug_private_access.sh [options]

Collect a best-effort debug report for the snowbridge private SMB + WireGuard +
File Browser path.

Options:
  --output PATH       Report path. Default: reports/private-access-debug-<timestamp>.log
  --wg-interface IF   WireGuard interface name. Default: wg0
  --hostname HOST     Private HTTPS hostname to probe. Default: files.snowbridge.internal
  --private-ip IP     WireGuard host IP to probe. Default: 10.99.0.1
  --help              Show this help text.

Run this script with sudo so it can read systemd, journal, socket, and firewall
state. The report path is ignored by git via reports/.
EOF
}

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || fail "run as root so the report can include system services, ports, logs, and firewall state"
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

compose_cmd_text() {
  if command_exists podman-compose; then
    printf 'podman-compose\n'
    return
  fi
  if command_exists docker && docker compose version >/dev/null 2>&1; then
    printf 'docker compose\n'
    return
  fi
  if command_exists docker-compose; then
    printf 'docker-compose\n'
    return
  fi
  printf '\n'
}

append_header() {
  local title="$1"
  {
    printf '\n'
    printf '===== %s =====\n' "${title}"
  } >> "${REPORT_PATH}"
}

run_shell() {
  local description="$1"
  local command_text="$2"

  append_header "${description}"
  {
    printf '$ %s\n' "${command_text}"
    /bin/bash -lc "${command_text}"
    local exit_code=$?
    printf '\n[exit %s]\n' "${exit_code}"
  } >> "${REPORT_PATH}" 2>&1
}

append_literal() {
  local description="$1"
  local text="$2"
  append_header "${description}"
  printf '%s\n' "${text}" >> "${REPORT_PATH}"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --output)
        REPORT_PATH="$2"
        shift 2
        ;;
      --wg-interface)
        WG_INTERFACE="$2"
        shift 2
        ;;
      --hostname)
        PRIVATE_HOSTNAME="$2"
        shift 2
        ;;
      --private-ip)
        PRIVATE_IP="$2"
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
}

main() {
  parse_args "$@"
  require_root

  mkdir -p "$(dirname "${REPORT_PATH}")"
  : > "${REPORT_PATH}"

  append_literal "Report Metadata" "$(cat <<EOF
timestamp: $(date --iso-8601=seconds)
repo_root: ${REPO_ROOT}
report_path: ${REPORT_PATH}
wg_interface: ${WG_INTERFACE}
private_hostname: ${PRIVATE_HOSTNAME}
private_ip: ${PRIVATE_IP}
EOF
)"

  run_shell "System Identity" "hostnamectl status || true"
  run_shell "Kernel and User" "uname -a && id && whoami"

  run_shell "WireGuard Service Status" "systemctl status wg-quick@${WG_INTERFACE}.service --no-pager || true"
  run_shell "dnsmasq Service Status" "systemctl status dnsmasq.service --no-pager || true"
  run_shell "Samba Service Status" "systemctl status smb.service --no-pager || true"

  run_shell "Recent Journal" "journalctl -u wg-quick@${WG_INTERFACE}.service -u dnsmasq.service -u smb.service -n 200 --no-pager || true"

  run_shell "Interface Addresses" "ip -brief address show ${WG_INTERFACE} || true"
  run_shell "WireGuard State" "wg show ${WG_INTERFACE} || true"
  run_shell "Routing" "ip route show table main || true"
  run_shell "Route To Private IP" "ip route get ${PRIVATE_IP} || true"

  run_shell "Listeners" "ss -ltnup || true"
  run_shell "Focused Ports" "ss -ltnup | rg ':(53|80|443|445|51820)\\b' || true"

  run_shell "firewalld Active Zones" "firewall-cmd --get-active-zones || true"
  run_shell "firewalld Default Zone" "firewall-cmd --get-default-zone || true"
  run_shell "firewalld Full Config" "firewall-cmd --list-all || true"
  run_shell "firewalld Trusted Zone" "firewall-cmd --zone=trusted --list-all || true"

  run_shell "dnsmasq Config" "sed -n '1,200p' /etc/dnsmasq.d/snowbridge-wireguard.conf || true"
  run_shell "dnsmasq Drop-in" "sed -n '1,200p' /etc/systemd/system/dnsmasq.service.d/snowbridge-wireguard.conf || true"

  run_shell "Runtime Web Env" "sed -n '1,200p' '${ENV_FILE}' || true"
  run_shell "Private Caddyfile" "sed -n '1,200p' '${PRIVATE_CADDYFILE}' || true"

  run_shell "Host Resolution" "getent hosts ${PRIVATE_HOSTNAME} || true"
  run_shell "resolvectl Status" "resolvectl status || true"
  run_shell "Local DNS Query" "command -v dig >/dev/null 2>&1 && dig @${PRIVATE_IP} ${PRIVATE_HOSTNAME} +short || command -v host >/dev/null 2>&1 && host ${PRIVATE_HOSTNAME} ${PRIVATE_IP} || true"

  run_shell "HTTPS Probe via Loopback" "curl -kI --resolve ${PRIVATE_HOSTNAME}:443:127.0.0.1 https://${PRIVATE_HOSTNAME} || true"
  run_shell "HTTPS Probe via Private IP" "curl -kI --resolve ${PRIVATE_HOSTNAME}:443:${PRIVATE_IP} https://${PRIVATE_HOSTNAME} || true"
  run_shell "HTTPS Probe via Raw IP" "curl -kI https://${PRIVATE_IP} || true"

  run_shell "SMB Config Validation" "testparm -s /etc/samba/smb.conf || true"
  run_shell "Share Bind Mounts" "mount | grep '/srv/snowbridge/share/' || true"

  local compose_text
  compose_text="$(compose_cmd_text)"
  if [[ -n "${compose_text}" ]]; then
    run_shell "Compose PS" "${compose_text} -f '${COMPOSE_FILE}' ps || true"
    run_shell "Compose Logs" "${compose_text} -f '${COMPOSE_FILE}' logs --tail=200 || true"
  else
    append_literal "Compose Frontend" "No supported compose frontend found in PATH."
  fi

  if command_exists podman; then
    run_shell "Podman Containers" "podman ps -a --filter name=snowbridge || true"
    run_shell "Caddy Logs" "podman logs --tail=200 snowbridge-caddy || true"
    run_shell "File Browser Logs" "podman logs --tail=200 snowbridge-filebrowser || true"
  fi

  log "wrote ${REPORT_PATH}"
}

main "$@"
