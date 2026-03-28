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

usage() {
  cat <<'EOF'
Usage: setup_caddy_filebrowser.sh [options]

Prepare and launch the optional File Browser + Caddy stack with Docker Compose.

Options:
  --mode private-vpn|public   Choose which Caddy template to initialize or expect.
  --init-local-configs        Copy the example env and chosen Caddyfile to local-only files.
  --env-file PATH             Local env file to use. Default: config/web/filebrowser/filebrowser.env.local
  --compose-file PATH         Compose file to use. Default: config/web/filebrowser/docker-compose.example.yml
  --skip-up                   Validate and prepare directories, but do not run docker compose up -d.
  --help                      Show this help text.

Typical flow:
  ./scripts/setup_caddy_filebrowser.sh --init-local-configs --mode private-vpn
  # edit config/web/filebrowser/filebrowser.env.local and the chosen Caddyfile
  sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn
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
  docker compose "$@"
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
require_command docker
require_command install

compose_cmd version >/dev/null
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
[[ -f "${CADDYFILE_PATH}" ]] || fail "Caddyfile does not exist: ${CADDYFILE_PATH}"

install -d -m 0750 -o "${SNOWBRIDGE_UID}" -g "${SNOWBRIDGE_GID}" "${FILEBROWSER_DB_DIR}"
install -d -m 0750 -o "${SNOWBRIDGE_UID}" -g "${SNOWBRIDGE_GID}" "${FILEBROWSER_CONFIG_DIR}"
install -d -m 0750 "${CADDY_DATA_DIR}"
install -d -m 0750 "${CADDY_CONFIG_DIR}"

compose_cmd --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" config >/dev/null
log "validated docker compose configuration"

if (( SKIP_UP == 0 )); then
  compose_cmd --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d
  log "started File Browser + Caddy stack"
fi

log "next checks:"
log "  docker compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} ps"
log "  docker compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} logs --tail=100"
