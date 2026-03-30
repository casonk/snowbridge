#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SERVER_EXAMPLE="${REPO_ROOT}/config/access/wireguard/wg0-server.example.conf"
SERVER_LOCAL_DEFAULT="${REPO_ROOT}/config/access/wireguard/wg0-server.local.conf"
IPHONE_EXAMPLE="${REPO_ROOT}/config/access/wireguard/iphone-peer.example.conf"
IPHONE_LOCAL_DEFAULT="${REPO_ROOT}/config/access/wireguard/iphone-peer.local.conf"
PRIVATE_CADDY_EXAMPLE="${REPO_ROOT}/config/web/caddy/Caddyfile.private-vpn.example"
PRIVATE_CADDY_LOCAL_DEFAULT="${REPO_ROOT}/config/web/caddy/Caddyfile.private-vpn.local"
SERVER_DEST_DEFAULT="/etc/wireguard/wg0.conf"
IP_FORWARD_SYSCTL="/etc/sysctl.d/99-snowbridge-wireguard.conf"
WIREGUARD_DNS_DEST_DEFAULT="/etc/dnsmasq.d/snowbridge-wireguard.conf"
WIREGUARD_DNS_DROPIN_DEFAULT="/etc/systemd/system/dnsmasq.service.d/snowbridge-wireguard.conf"
WIREGUARD_DNS_HOSTNAME_DEFAULT="files.snowbridge.internal"

SERVER_CONFIG="${SERVER_LOCAL_DEFAULT}"
IPHONE_CONFIG="${IPHONE_LOCAL_DEFAULT}"
SERVER_DEST="${SERVER_DEST_DEFAULT}"
WIREGUARD_DNS_DEST="${WIREGUARD_DNS_DEST_DEFAULT}"
WIREGUARD_DNS_DROPIN="${WIREGUARD_DNS_DROPIN_DEFAULT}"
INIT_LOCAL_CONFIGS=0
ENABLE_IP_FORWARD=0
SKIP_START=0
PRINT_IPHONE_QR=0
QR_OUTPUT=""
GENERATE_MISSING_KEYS=0
SKIP_DNS=0
WIREGUARD_DNS_HOSTNAME=""
SKIP_FIREWALL=0
WIREGUARD_FIREWALL_ZONE="trusted"

usage() {
  cat <<'EOF'
Usage: setup_wireguard.sh [options]

Set up the snowbridge WireGuard server from local-only config files.

Options:
  --init-local-configs     Copy the example configs to local-only .local files.
  --server-config PATH     Local server config to install.
  --iphone-config PATH     Local iPhone peer config for QR rendering.
  --server-dest PATH       Destination server config path. Default: /etc/wireguard/wg0.conf
  --dns-dest PATH          Destination dnsmasq config path. Default: /etc/dnsmasq.d/snowbridge-wireguard.conf
  --dns-hostname HOST      Private hostname to publish to WireGuard clients. Default: first hostname from the
                           private Caddy config, or files.snowbridge.internal when none is found.
  --generate-missing-keys  Generate and write missing server/iPhone key pairs when the
                           paired placeholders are still present.
  --enable-ip-forward      Write a sysctl drop-in enabling IPv4 and IPv6 forwarding.
  --skip-dns               Do not install or restart the dnsmasq split-DNS helper for WireGuard clients.
  --skip-firewall          Do not update firewalld for the WireGuard interface.
  --firewall-zone ZONE     firewalld zone to assign to the WireGuard interface. Default: trusted
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

warn() {
  printf 'warning: %s\n' "$*" >&2
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

  if (( SKIP_DNS == 0 )) && ! command -v dnsmasq >/dev/null 2>&1; then
    missing_packages+=(dnsmasq)
  fi

  if (( PRINT_IPHONE_QR == 1 )) || [[ -n "${QR_OUTPUT}" ]]; then
    if ! command -v qrencode >/dev/null 2>&1; then
      missing_packages+=(qrencode)
    fi
  fi

  if [[ -f "${IPHONE_CONFIG}" ]] && endpoint_needs_autofill; then
    if ! command -v curl >/dev/null 2>&1; then
      missing_packages+=(curl)
    fi
  fi

  if (( ${#missing_packages[@]} == 0 )); then
    return
  fi

  install_os_packages "${missing_packages[@]}"

  command -v wg-quick >/dev/null 2>&1 || fail "wg-quick still missing after package install"
  if (( SKIP_DNS == 0 )); then
    command -v dnsmasq >/dev/null 2>&1 || fail "dnsmasq still missing after package install"
  fi
  if (( PRINT_IPHONE_QR == 1 )) || [[ -n "${QR_OUTPUT}" ]]; then
    command -v qrencode >/dev/null 2>&1 || fail "qrencode still missing after package install"
  fi
  if [[ -f "${IPHONE_CONFIG}" ]] && endpoint_needs_autofill; then
    command -v curl >/dev/null 2>&1 || fail "curl still missing after package install"
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
  if contains_placeholders "${path}"; then
    fail "${label} still contains placeholder values: ${path}"
  fi
}

endpoint_needs_autofill() {
  local endpoint

  endpoint="$(extract_first_config_value "${IPHONE_CONFIG}" "Endpoint")"

  [[ -z "${endpoint}" ]] && return 0
  [[ "${endpoint}" == "<wireguard-endpoint-hostname-or-ip>:51820" ]] && return 0
  [[ "${endpoint}" =~ ^vpn\.example\.com(:[0-9]+)?$ ]] && return 0

  return 1
}

format_endpoint_host() {
  local host="$1"

  if [[ "${host}" == *:* ]] && [[ ! "${host}" =~ ^\[.*\]$ ]]; then
    printf '[%s]\n' "${host}"
    return
  fi

  printf '%s\n' "${host}"
}

detect_public_ip() {
  local url
  local ip_candidate

  require_command curl

  for url in \
    "https://api.ipify.org" \
    "https://ifconfig.me/ip" \
    "https://icanhazip.com"
  do
    ip_candidate="$(curl -fsS --max-time 10 "${url}" 2>/dev/null | tr -d '[:space:]')" || continue
    [[ -z "${ip_candidate}" ]] && continue
    if [[ "${ip_candidate}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
      printf '%s\n' "${ip_candidate}"
      return
    fi
    if [[ "${ip_candidate}" =~ ^[0-9A-Fa-f:]+$ ]] && [[ "${ip_candidate}" == *:* ]]; then
      printf '%s\n' "${ip_candidate}"
      return
    fi
  done

  fail "unable to detect the current public IP automatically; set Endpoint manually in ${IPHONE_CONFIG}"
}

autofill_iphone_endpoint_if_needed() {
  local public_ip
  local listen_port
  local endpoint

  [[ -f "${IPHONE_CONFIG}" ]] || fail "iPhone peer config not found: ${IPHONE_CONFIG}"

  if ! endpoint_needs_autofill; then
    return
  fi

  public_ip="$(detect_public_ip)"
  listen_port="$(extract_first_config_value "${SERVER_CONFIG}" "ListenPort")"
  [[ -n "${listen_port}" ]] || listen_port="51820"
  endpoint="$(format_endpoint_host "${public_ip}"):${listen_port}"

  sed -i "s|^[[:space:]]*Endpoint[[:space:]]*=.*$|Endpoint = ${endpoint}|" "${IPHONE_CONFIG}"
  warn "auto-set the iPhone peer Endpoint to the current public IP (${public_ip}) in ${IPHONE_CONFIG}; replace it with a stable DNS name or other stable public endpoint if this WAN address can change"
}

check_iphone_export_config() {
  local endpoint

  check_local_config "${IPHONE_CONFIG}" "iPhone peer config"
  endpoint="$(extract_first_config_value "${IPHONE_CONFIG}" "Endpoint")"
  [[ -n "${endpoint}" ]] || fail "iPhone peer config is missing Endpoint: ${IPHONE_CONFIG}"

  if endpoint_needs_autofill; then
    fail "iPhone peer config still has an incomplete Endpoint (${endpoint}); replace it with the real VPN hostname or public IP or let the installer auto-fill it before QR export: ${IPHONE_CONFIG}"
  fi
}

replace_placeholder_in_file() {
  local path="$1"
  local placeholder="$2"
  local value="$3"
  sed -i "s|${placeholder}|${value}|g" "${path}"
}

extract_first_config_value() {
  local path="$1"
  local key_name="$2"
  awk -F '=' -v key_name="${key_name}" '
    $1 ~ "^[[:space:]]*" key_name "[[:space:]]*$" {
      value = $2
      sub(/^[[:space:]]+/, "", value)
      sub(/[[:space:]]+$/, "", value)
      print value
      exit
    }
  ' "${path}"
}

extract_first_caddy_hostname() {
  local caddyfile_path="$1"
  [[ -f "${caddyfile_path}" ]] || return 1

  awk '
    function emit_host(token) {
      host = token
      sub(/^[A-Za-z0-9+.-]+:\/\//, "", host)
      sub(/:[0-9]+$/, "", host)
      gsub(/^\[|\]$/, "", host)
      if (host == "") {
        return
      }
      if (host ~ /^([0-9]{1,3}\.){3}[0-9]{1,3}$/) {
        return
      }
      if (host ~ /^[0-9A-Fa-f:]+$/ && host ~ /:/) {
        return
      }
      print host
      exit
    }

    /^[[:space:]]*#/ { next }
    /^[[:space:]]*$/ { next }
    /^[[:space:]]*{$/ { in_global = 1; next }
    in_global && /^[[:space:]]*}/ { in_global = 0; next }
    !in_global && /{$/ {
      site = $0
      sub(/[[:space:]]*{[[:space:]]*$/, "", site)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", site)
      count = split(site, parts, /[[:space:]]*,[[:space:]]*|[[:space:]]+/)
      for (i = 1; i <= count; i++) {
        emit_host(parts[i])
      }
    }
  ' "${caddyfile_path}"
}

resolve_wireguard_dns_hostname() {
  local candidate

  if [[ -n "${WIREGUARD_DNS_HOSTNAME}" ]]; then
    printf '%s\n' "${WIREGUARD_DNS_HOSTNAME}"
    return
  fi

  for candidate in "${PRIVATE_CADDY_LOCAL_DEFAULT}" "${PRIVATE_CADDY_EXAMPLE}"; do
    candidate="$(extract_first_caddy_hostname "${candidate}" || true)"
    if [[ -n "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return
    fi
  done

  printf '%s\n' "${WIREGUARD_DNS_HOSTNAME_DEFAULT}"
}

extract_first_address_from_list() {
  local raw_value="$1"
  local first_address

  first_address="${raw_value%%,*}"
  first_address="${first_address%% *}"
  first_address="${first_address#${first_address%%[![:space:]]*}}"
  first_address="${first_address%${first_address##*[![:space:]]}}"
  printf '%s\n' "${first_address}"
}

extract_wireguard_server_ip() {
  local address_value
  local primary_address

  address_value="$(extract_first_config_value "${SERVER_CONFIG}" "Address")"
  [[ -n "${address_value}" ]] || fail "server config is missing Address: ${SERVER_CONFIG}"

  primary_address="$(extract_first_address_from_list "${address_value}")"
  [[ -n "${primary_address}" ]] || fail "server config Address is empty: ${SERVER_CONFIG}"
  printf '%s\n' "${primary_address%%/*}"
}

install_wireguard_dns() {
  local dns_hostname
  local dns_ip
  local iphone_dns
  local dropin_dir

  (( SKIP_DNS == 1 )) && return

  dns_hostname="$(resolve_wireguard_dns_hostname)"
  [[ -n "${dns_hostname}" ]] || fail "unable to determine WireGuard DNS hostname"
  dns_ip="$(extract_wireguard_server_ip)"
  iphone_dns="$(extract_first_config_value "${IPHONE_CONFIG}" "DNS")"

  if [[ -z "${iphone_dns}" ]]; then
    warn "iPhone peer config has no DNS setting; add DNS = ${dns_ip} to ${IPHONE_CONFIG} so WireGuard clients resolve ${dns_hostname}"
  elif [[ "${iphone_dns}" != "${dns_ip}" ]]; then
    warn "iPhone peer config DNS (${iphone_dns}) does not match the server WireGuard IP (${dns_ip}); clients must use a DNS server that resolves ${dns_hostname}"
  fi

  install -d "$(dirname "${WIREGUARD_DNS_DEST}")"
  cat > "${WIREGUARD_DNS_DEST}" <<EOF
# managed by snowbridge setup_wireguard.sh
interface=${INTERFACE_NAME}
listen-address=${dns_ip}
bind-interfaces
no-hosts
host-record=${dns_hostname},${dns_ip}
EOF
  log "installed ${WIREGUARD_DNS_DEST}"

  dropin_dir="$(dirname "${WIREGUARD_DNS_DROPIN}")"
  install -d "${dropin_dir}"
  cat > "${WIREGUARD_DNS_DROPIN}" <<EOF
[Unit]
After=wg-quick@${INTERFACE_NAME}.service
Wants=wg-quick@${INTERFACE_NAME}.service
EOF
  systemctl daemon-reload
  log "installed ${WIREGUARD_DNS_DROPIN}"

  if (( SKIP_START == 0 )); then
    systemctl enable --now dnsmasq.service
    systemctl restart dnsmasq.service
    log "enabled and restarted dnsmasq.service for ${dns_hostname} -> ${dns_ip}"
  else
    systemctl enable dnsmasq.service
    log "enabled dnsmasq.service; start it after ${INTERFACE_NAME} is up to serve ${dns_hostname} -> ${dns_ip}"
  fi
}

install_wireguard_firewall() {
  (( SKIP_FIREWALL == 1 )) && return

  if ! command -v firewall-cmd >/dev/null 2>&1; then
    warn "firewall-cmd not found; skipping firewalld integration for ${INTERFACE_NAME}"
    return
  fi

  if ! systemctl is-active --quiet firewalld.service; then
    warn "firewalld.service is not active; skipping firewalld integration for ${INTERFACE_NAME}"
    return
  fi

  if firewall-cmd --permanent --zone="${WIREGUARD_FIREWALL_ZONE}" --query-interface="${INTERFACE_NAME}" >/dev/null 2>&1; then
    firewall-cmd --zone="${WIREGUARD_FIREWALL_ZONE}" --add-interface="${INTERFACE_NAME}" >/dev/null 2>&1 || true
    log "firewalld already maps ${INTERFACE_NAME} to zone ${WIREGUARD_FIREWALL_ZONE}"
  else
    firewall-cmd --permanent --zone="${WIREGUARD_FIREWALL_ZONE}" --add-interface="${INTERFACE_NAME}" >/dev/null
    firewall-cmd --zone="${WIREGUARD_FIREWALL_ZONE}" --add-interface="${INTERFACE_NAME}" >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null
    log "assigned ${INTERFACE_NAME} to firewalld zone ${WIREGUARD_FIREWALL_ZONE}"
  fi
}

derive_public_key() {
  local private_key="$1"
  printf '%s' "${private_key}" | wg pubkey
}

generate_key_pair() {
  local private_key
  local public_key
  private_key="$(wg genkey)"
  public_key="$(derive_public_key "${private_key}")"
  printf '%s\n%s\n' "${private_key}" "${public_key}"
}

sync_server_key_pair() {
  local server_private_key
  local server_public_key
  local generated_pair

  server_private_key="$(extract_first_config_value "${SERVER_CONFIG}" "PrivateKey")"
  server_public_key="$(extract_first_config_value "${IPHONE_CONFIG}" "PublicKey")"

  if [[ "${server_private_key}" == "<server-private-key>" ]]; then
    if [[ "${server_public_key}" == "<server-public-key>" ]]; then
      generated_pair="$(generate_key_pair)"
      server_private_key="$(printf '%s\n' "${generated_pair}" | sed -n '1p')"
      server_public_key="$(printf '%s\n' "${generated_pair}" | sed -n '2p')"
      replace_placeholder_in_file "${SERVER_CONFIG}" "<server-private-key>" "${server_private_key}"
      replace_placeholder_in_file "${IPHONE_CONFIG}" "<server-public-key>" "${server_public_key}"
      log "generated server WireGuard key pair"
      return
    fi
    fail "server private key is still a placeholder while iPhone config already has a server public key; set the matching private key manually or restore <server-public-key> so the script can generate a fresh pair"
  fi

  if [[ "${server_public_key}" == "<server-public-key>" ]]; then
    server_public_key="$(derive_public_key "${server_private_key}")"
    replace_placeholder_in_file "${IPHONE_CONFIG}" "<server-public-key>" "${server_public_key}"
    log "derived server public key from existing server private key"
  fi
}

sync_iphone_key_pair() {
  local iphone_private_key
  local iphone_public_key
  local generated_pair

  iphone_private_key="$(extract_first_config_value "${IPHONE_CONFIG}" "PrivateKey")"
  iphone_public_key="$(extract_first_config_value "${SERVER_CONFIG}" "PublicKey")"

  if [[ "${iphone_private_key}" == "<iphone-private-key>" ]]; then
    if [[ "${iphone_public_key}" == "<iphone-public-key>" ]]; then
      generated_pair="$(generate_key_pair)"
      iphone_private_key="$(printf '%s\n' "${generated_pair}" | sed -n '1p')"
      iphone_public_key="$(printf '%s\n' "${generated_pair}" | sed -n '2p')"
      replace_placeholder_in_file "${IPHONE_CONFIG}" "<iphone-private-key>" "${iphone_private_key}"
      replace_placeholder_in_file "${SERVER_CONFIG}" "<iphone-public-key>" "${iphone_public_key}"
      log "generated iPhone WireGuard key pair"
      return
    fi
    fail "iPhone private key is still a placeholder while server config already has an iPhone public key; set the matching private key manually or restore <iphone-public-key> so the script can generate a fresh pair"
  fi

  if [[ "${iphone_public_key}" == "<iphone-public-key>" ]]; then
    iphone_public_key="$(derive_public_key "${iphone_private_key}")"
    replace_placeholder_in_file "${SERVER_CONFIG}" "<iphone-public-key>" "${iphone_public_key}"
    log "derived iPhone public key from existing iPhone private key"
  fi
}

generate_missing_keys_if_needed() {
  [[ -f "${SERVER_CONFIG}" ]] || fail "server config not found: ${SERVER_CONFIG}"
  [[ -f "${IPHONE_CONFIG}" ]] || fail "iPhone peer config not found: ${IPHONE_CONFIG}"

  if ! grep -Eq '<(server-private-key|server-public-key|iphone-private-key|iphone-public-key)>' "${SERVER_CONFIG}" "${IPHONE_CONFIG}"; then
    return
  fi

  require_command wg
  sync_server_key_pair
  sync_iphone_key_pair
}

render_iphone_qr() {
  (( PRINT_IPHONE_QR == 0 )) && [[ -z "${QR_OUTPUT}" ]] && return

  require_command qrencode
  check_iphone_export_config

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
    --dns-dest)
      WIREGUARD_DNS_DEST="$2"
      shift 2
      ;;
    --dns-hostname)
      WIREGUARD_DNS_HOSTNAME="$2"
      shift 2
      ;;
    --generate-missing-keys)
      GENERATE_MISSING_KEYS=1
      shift
      ;;
    --enable-ip-forward)
      ENABLE_IP_FORWARD=1
      shift
      ;;
    --skip-dns)
      SKIP_DNS=1
      shift
      ;;
    --skip-firewall)
      SKIP_FIREWALL=1
      shift
      ;;
    --firewall-zone)
      WIREGUARD_FIREWALL_ZONE="$2"
      shift 2
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
  if (( GENERATE_MISSING_KEYS == 1 )); then
    require_command wg
    generate_missing_keys_if_needed
  fi
  log "edit the local configs, then rerun with sudo to install them"
  exit 0
fi

require_root
require_command install
require_command systemctl
install_runtime_packages_if_needed
generate_missing_keys_if_needed
autofill_iphone_endpoint_if_needed

check_local_config "${SERVER_CONFIG}" "server config"
if (( PRINT_IPHONE_QR == 1 )) || [[ -n "${QR_OUTPUT}" ]]; then
  check_iphone_export_config
fi
INTERFACE_NAME="$(basename "${SERVER_DEST}" .conf)"

install -d /etc/wireguard
install -m 600 "${SERVER_CONFIG}" "${SERVER_DEST}"
log "installed ${SERVER_DEST}"

if (( ENABLE_IP_FORWARD == 1 )); then
  printf 'net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n' > "${IP_FORWARD_SYSCTL}"
  sysctl -p "${IP_FORWARD_SYSCTL}" >/dev/null
  log "updated ${IP_FORWARD_SYSCTL}"
fi

render_iphone_qr

if (( SKIP_START == 0 )); then
  systemctl enable --now "wg-quick@${INTERFACE_NAME}.service"
  systemctl restart "wg-quick@${INTERFACE_NAME}.service"
  log "enabled and restarted wg-quick@${INTERFACE_NAME}.service"
fi

install_wireguard_dns
install_wireguard_firewall

log "next checks:"
log "  sudo wg show ${INTERFACE_NAME}"
log "  sudo systemctl status wg-quick@${INTERFACE_NAME}.service"
if (( SKIP_DNS == 0 )); then
  log "  sudo systemctl status dnsmasq.service"
fi
if (( SKIP_FIREWALL == 0 )); then
  log "  sudo firewall-cmd --zone=${WIREGUARD_FIREWALL_ZONE} --list-all"
fi
