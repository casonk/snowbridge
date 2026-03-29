#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

COMPOSE_FILE_DEFAULT="${REPO_ROOT}/config/web/filebrowser/docker-compose.example.yml"
ENV_EXAMPLE="${REPO_ROOT}/config/web/filebrowser/filebrowser.env.example"
ENV_LOCAL_DEFAULT="${REPO_ROOT}/config/web/filebrowser/filebrowser.env.local"
PRIVATE_CADDY_EXAMPLE="${REPO_ROOT}/config/web/caddy/Caddyfile.private-vpn.example"
PRIVATE_CADDY_LOCAL_DEFAULT="${REPO_ROOT}/config/web/caddy/Caddyfile.private-vpn.local"
PUBLIC_CADDY_EXAMPLE="${REPO_ROOT}/config/web/caddy/Caddyfile.public.example"
PUBLIC_CADDY_LOCAL_DEFAULT="${REPO_ROOT}/config/web/caddy/Caddyfile.public.local"

MODE="private-vpn"
ENV_FILE="${ENV_LOCAL_DEFAULT}"
COMPOSE_FILE="${COMPOSE_FILE_DEFAULT}"
INIT_LOCAL_CONFIGS=0
SKIP_UP=0
RECREATE=0
BOOTSTRAP_LOCAL_BROWSER=0
COMPOSE_CMD=()
COMPOSE_CMD_TEXT=""

usage() {
  cat <<'EOF'
Usage: setup_caddy_filebrowser.sh [options]

Prepare and launch the optional File Browser + Caddy stack with a supported
Compose frontend.

Options:
  --mode private-vpn|public   Choose which Caddy template to initialize or expect.
  --init-local-configs        Copy the example env and chosen Caddyfile to local-only files.
  --env-file PATH             Local env file to use. Default: config/web/filebrowser/filebrowser.env.local
  --compose-file PATH         Compose file to use. Default: config/web/filebrowser/docker-compose.example.yml
  --recreate                  Force container recreation on the next compose up run.
  --bootstrap-local-browser   Install the Caddy local CA into host trust and add a local
                              hosts entry for the configured private hostname if needed.
  --skip-up                   Validate and prepare directories, but do not run docker compose up -d.
  --help                      Show this help text.

This installer will install missing container runtime and Compose packages
automatically when a supported package manager is available. Currently
supported: dnf, apt-get, yum.

Typical flow:
  ./scripts/setup_caddy_filebrowser.sh --init-local-configs --mode private-vpn
  # edit config/web/filebrowser/filebrowser.env.local and the chosen Caddyfile
  sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn
  # if mounts, labels, ports, or images changed:
  sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn --recreate
  # for desktop-browser access on this host:
  sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn --bootstrap-local-browser
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

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || fail "run as root so the host data directories can be created"
}

ensure_mode() {
  case "${MODE}" in
    private-vpn|public) ;;
    *)
      fail "invalid mode: ${MODE}"
      ;;
  esac
}

compose_cmd() {
  (( ${#COMPOSE_CMD[@]} > 0 )) || fail "compose command not initialized"
  "${COMPOSE_CMD[@]}" "$@"
}

docker_is_podman_wrapper() {
  command_exists docker || return 1
  docker --help 2>&1 | grep -q 'Emulate Docker CLI using podman'
}

docker_compose_available() {
  command_exists docker && docker compose version >/dev/null 2>&1
}

docker_compose_legacy_available() {
  command_exists docker-compose && docker-compose version >/dev/null 2>&1
}

podman_compose_available() {
  command_exists podman-compose && podman-compose version >/dev/null 2>&1
}

set_compose_command() {
  if podman_compose_available && (command_exists podman || docker_is_podman_wrapper); then
    COMPOSE_CMD=(podman-compose)
    COMPOSE_CMD_TEXT="podman-compose"
    return 0
  fi

  if docker_compose_available; then
    COMPOSE_CMD=(docker compose)
    COMPOSE_CMD_TEXT="docker compose"
    return 0
  fi

  if docker_compose_legacy_available; then
    COMPOSE_CMD=(docker-compose)
    COMPOSE_CMD_TEXT="docker-compose"
    return 0
  fi

  if podman_compose_available; then
    COMPOSE_CMD=(podman-compose)
    COMPOSE_CMD_TEXT="podman-compose"
    return 0
  fi

  return 1
}

find_package_manager() {
  if command_exists dnf; then
    printf 'dnf\n'
    return
  fi
  if command_exists apt-get; then
    printf 'apt-get\n'
    return
  fi
  if command_exists yum; then
    printf 'yum\n'
    return
  fi
  fail "missing container runtime packages and no supported package manager found (supported: dnf, apt-get, yum)"
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
  if set_compose_command; then
    return
  fi

  case "$(find_package_manager)" in
    dnf|yum)
      install_os_packages podman podman-compose
      ;;
    apt-get)
      if command_exists podman || docker_is_podman_wrapper; then
        install_os_packages podman podman-compose
      elif command_exists docker; then
        install_os_packages docker-compose-plugin
      else
        install_os_packages docker.io docker-compose-plugin
      fi
      ;;
    *)
      fail "unsupported package manager"
      ;;
  esac

  set_compose_command || fail "no supported Compose frontend available after package install"
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

replace_setting() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp
  tmp="$(mktemp)"
  sed "s|^${key}=.*|${key}=${value}|" "${file}" > "${tmp}"
  mv "${tmp}" "${file}"
}

extract_caddy_site_hostname() {
  awk '
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*$/ { next }
    /^[[:space:]]*{$/ { in_global = 1; next }
    in_global && /^[[:space:]]*}/ { in_global = 0; next }
    !in_global && /{$/ {
      site = $0
      sub(/[[:space:]]*{[[:space:]]*$/, "", site)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", site)
      split(site, parts, /[[:space:]]*,[[:space:]]*|[[:space:]]+/)
      host = parts[1]
      sub(/^[A-Za-z0-9+.-]+:\/\//, "", host)
      sub(/:[0-9]+$/, "", host)
      print host
      exit
    }
  ' "${CADDYFILE_PATH}"
}

system_ca_anchor_path() {
  if command_exists update-ca-trust; then
    printf '/etc/pki/ca-trust/source/anchors/snowbridge-caddy-local-root.crt\n'
    return
  fi
  if command_exists update-ca-certificates; then
    printf '/usr/local/share/ca-certificates/snowbridge-caddy-local-root.crt\n'
    return
  fi
  fail "no supported system CA trust updater found (supported: update-ca-trust, update-ca-certificates)"
}

ensure_ca_trust_tools_if_needed() {
  if command_exists update-ca-trust || command_exists update-ca-certificates; then
    return
  fi

  case "$(find_package_manager)" in
    dnf|yum)
      install_os_packages ca-certificates
      ;;
    apt-get)
      install_os_packages ca-certificates
      ;;
    *)
      fail "unsupported package manager"
      ;;
  esac

  command_exists update-ca-trust || command_exists update-ca-certificates || \
    fail "system CA trust tools still missing after package install"
}

refresh_system_ca_trust() {
  if command_exists update-ca-trust; then
    update-ca-trust extract
    return
  fi
  if command_exists update-ca-certificates; then
    update-ca-certificates
    return
  fi
  fail "no supported system CA trust updater found"
}

install_local_caddy_ca() {
  local caddy_root_ca
  local anchor_path

  caddy_root_ca="${CADDY_DATA_DIR}/caddy/pki/authorities/local/root.crt"
  [[ -f "${caddy_root_ca}" ]] || fail "Caddy local root CA not found at ${caddy_root_ca}; start the stack first so Caddy can generate it"

  ensure_ca_trust_tools_if_needed
  anchor_path="$(system_ca_anchor_path)"
  install -D -m 0644 "${caddy_root_ca}" "${anchor_path}"
  refresh_system_ca_trust
  log "installed local Caddy root CA into host trust: ${anchor_path}"
}

add_local_hosts_entry_if_needed() {
  local site_hostname

  site_hostname="$(extract_caddy_site_hostname)"
  [[ -n "${site_hostname}" ]] || fail "could not determine site hostname from ${CADDYFILE_PATH}"

  if getent hosts "${site_hostname}" >/dev/null 2>&1; then
    log "hostname already resolves locally: ${site_hostname}"
    return
  fi

  printf '127.0.0.1 %s\n' "${site_hostname}" >> /etc/hosts
  log "added local hosts entry: 127.0.0.1 ${site_hostname}"
}

bootstrap_local_browser_access() {
  add_local_hosts_entry_if_needed
  install_local_caddy_ca
}

selected_caddy_example() {
  if [[ "${MODE}" == "public" ]]; then
    printf '%s\n' "${PUBLIC_CADDY_EXAMPLE}"
  else
    printf '%s\n' "${PRIVATE_CADDY_EXAMPLE}"
  fi
}

selected_caddy_local() {
  if [[ "${MODE}" == "public" ]]; then
    printf '%s\n' "${PUBLIC_CADDY_LOCAL_DEFAULT}"
  else
    printf '%s\n' "${PRIVATE_CADDY_LOCAL_DEFAULT}"
  fi
}

selected_caddy_runtime_path() {
  if [[ "${MODE}" == "public" ]]; then
    printf '/etc/caddy/Caddyfile.public\n'
  else
    printf '/etc/caddy/Caddyfile.private-vpn\n'
  fi
}

repair_caddyfile_path_if_needed() {
  local local_caddyfile
  local absolute_local_caddyfile
  local runtime_caddyfile_path

  [[ -f "${CADDYFILE_PATH}" ]] && return

  local_caddyfile="$(selected_caddy_local)"
  runtime_caddyfile_path="$(selected_caddy_runtime_path)"
  absolute_local_caddyfile="$(readlink -f "${local_caddyfile}" 2>/dev/null || true)"

  if [[ -n "${absolute_local_caddyfile}" ]] && [[ -f "${absolute_local_caddyfile}" ]]; then
    if [[ "${CADDYFILE_PATH}" == "${runtime_caddyfile_path}" ]]; then
      replace_setting "${ENV_FILE}" "CADDYFILE_PATH" "${absolute_local_caddyfile}"
      CADDYFILE_PATH="${absolute_local_caddyfile}"
      export CADDYFILE_PATH
      log "updated stale CADDYFILE_PATH in ${ENV_FILE} to ${absolute_local_caddyfile}"
      return
    fi
  fi

  fail "Caddyfile does not exist: ${CADDYFILE_PATH}. Run ./scripts/setup_caddy_filebrowser.sh --init-local-configs --mode ${MODE} or update CADDYFILE_PATH in ${ENV_FILE}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="$2"
      shift 2
      ;;
    --init-local-configs)
      INIT_LOCAL_CONFIGS=1
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
    --recreate)
      RECREATE=1
      shift
      ;;
    --bootstrap-local-browser)
      BOOTSTRAP_LOCAL_BROWSER=1
      shift
      ;;
    --skip-up)
      SKIP_UP=1
      shift
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

ensure_mode

if (( INIT_LOCAL_CONFIGS == 1 )); then
  local_caddyfile="$(selected_caddy_local)"
  copy_if_missing "${ENV_EXAMPLE}" "${ENV_FILE}"
  copy_if_missing "$(selected_caddy_example)" "${local_caddyfile}"
  absolute_caddyfile="$(readlink -f "${local_caddyfile}")"
  replace_setting "${ENV_FILE}" "CADDYFILE_PATH" "${absolute_caddyfile}"
  if [[ "${MODE}" == "public" ]]; then
    replace_setting "${ENV_FILE}" "CADDY_HTTP_BIND" "0.0.0.0"
    replace_setting "${ENV_FILE}" "CADDY_HTTPS_BIND" "0.0.0.0"
  else
    replace_setting "${ENV_FILE}" "CADDY_HTTP_BIND" "127.0.0.1"
    replace_setting "${ENV_FILE}" "CADDY_HTTPS_BIND" "127.0.0.1"
  fi
  log "edit ${ENV_FILE} and ${local_caddyfile}, then rerun with sudo"
  exit 0
fi

require_root
require_command install
install_runtime_packages_if_needed

[[ -f "${ENV_FILE}" ]] || fail "env file not found: ${ENV_FILE}"
[[ -f "${COMPOSE_FILE}" ]] || fail "compose file not found: ${COMPOSE_FILE}"

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

required_vars=(
  SNOWBRIDGE_UID
  SNOWBRIDGE_GID
  SNOWBRIDGE_SHARE_ROOT
  FILEBROWSER_HTTP_PORT
  FILEBROWSER_DB_DIR
  FILEBROWSER_CONFIG_DIR
  CADDYFILE_PATH
  CADDY_DATA_DIR
  CADDY_CONFIG_DIR
  CADDY_HTTP_BIND
  CADDY_HTTPS_BIND
)

for var_name in "${required_vars[@]}"; do
  [[ -n "${!var_name:-}" ]] || fail "missing ${var_name} in ${ENV_FILE}"
done

for absolute_path_var in \
  SNOWBRIDGE_SHARE_ROOT \
  FILEBROWSER_DB_DIR \
  FILEBROWSER_CONFIG_DIR \
  CADDYFILE_PATH \
  CADDY_DATA_DIR \
  CADDY_CONFIG_DIR; do
  [[ "${!absolute_path_var}" == /* ]] || fail "${absolute_path_var} must be an absolute path"
done

[[ -d "${SNOWBRIDGE_SHARE_ROOT}" ]] || fail "share root does not exist: ${SNOWBRIDGE_SHARE_ROOT}"
repair_caddyfile_path_if_needed

install -d -m 0750 -o "${SNOWBRIDGE_UID}" -g "${SNOWBRIDGE_GID}" "${FILEBROWSER_DB_DIR}"
install -d -m 0750 -o "${SNOWBRIDGE_UID}" -g "${SNOWBRIDGE_GID}" "${FILEBROWSER_CONFIG_DIR}"
install -d -m 0750 "${CADDY_DATA_DIR}"
install -d -m 0750 "${CADDY_CONFIG_DIR}"

compose_cmd -f "${COMPOSE_FILE}" config >/dev/null
log "validated ${COMPOSE_CMD_TEXT} configuration"

if (( SKIP_UP == 0 )); then
  up_args=(-f "${COMPOSE_FILE}" up -d)
  if (( RECREATE == 1 )); then
    up_args+=(--force-recreate)
  fi
  compose_cmd "${up_args[@]}"
  log "started File Browser + Caddy stack"
fi

if (( BOOTSTRAP_LOCAL_BROWSER == 1 )); then
  bootstrap_local_browser_access
fi

log "next checks:"
log "  sudo ${COMPOSE_CMD_TEXT} -f ${COMPOSE_FILE} ps"
log "  sudo ${COMPOSE_CMD_TEXT} -f ${COMPOSE_FILE} logs --tail=100"
