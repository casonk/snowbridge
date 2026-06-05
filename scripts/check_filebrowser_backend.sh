#!/usr/bin/env bash
# check_filebrowser_backend.sh - verify and optionally restart the File Browser backend

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_ROOT}/config/web/filebrowser/filebrowser.env.local"
COMPOSE_FILE="${REPO_ROOT}/config/web/filebrowser/docker-compose.local.yml"
if [[ ! -f "${COMPOSE_FILE}" ]]; then
  COMPOSE_FILE="${REPO_ROOT}/config/web/filebrowser/docker-compose.example.yml"
fi

URL=""
TIMEOUT="5"
WAIT_ATTEMPTS="1"
WAIT_SLEEP="2"
RESTART=0
COMPOSE_CMD=()
COMPOSE_CMD_TEXT=""

usage() {
  cat <<'EOF'
Usage: check_filebrowser_backend.sh [options]

Probe the local File Browser backend with a GET request. With --restart, start
the configured compose service if the backend is not returning the web UI.

Options:
  --restart               Run compose up -d when the backend probe fails.
  --env-file PATH         File Browser env file. Default: config/web/filebrowser/filebrowser.env.local
  --compose-file PATH     Compose file. Default: docker-compose.local.yml when present, otherwise example.
  --url URL               Probe URL. Default: http://127.0.0.1:${FILEBROWSER_HTTP_PORT}/
  --timeout SECONDS       curl max-time for each probe. Default: 5
  --wait-attempts COUNT   Probe attempts before returning. Default: 1
  --wait-sleep SECONDS    Delay between attempts. Default: 2
  --help                  Show this help text.

Use --restart from a root-owned systemd timer so the check can read the local
env file and start the same rootful compose service as start_snowbridge.sh.
EOF
}

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --restart)
        RESTART=1
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
      --url)
        URL="$2"
        shift 2
        ;;
      --timeout)
        TIMEOUT="$2"
        shift 2
        ;;
      --wait-attempts)
        WAIT_ATTEMPTS="$2"
        shift 2
        ;;
      --wait-sleep)
        WAIT_SLEEP="$2"
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
}

load_env_if_needed() {
  if [[ -n "${URL}" ]]; then
    return
  fi

  [[ -f "${ENV_FILE}" ]] || fail "env file not found: ${ENV_FILE}"
  [[ -r "${ENV_FILE}" ]] || fail "env file is not readable: ${ENV_FILE}; run with sudo or pass --url"

  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a

  URL="http://127.0.0.1:${FILEBROWSER_HTTP_PORT:-8080}/"
}

set_compose_command() {
  if command_exists podman-compose; then
    COMPOSE_CMD=(podman-compose)
    COMPOSE_CMD_TEXT="podman-compose"
    return
  fi
  if command_exists docker && docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
    COMPOSE_CMD_TEXT="docker compose"
    return
  fi
  if command_exists docker-compose; then
    COMPOSE_CMD=(docker-compose)
    COMPOSE_CMD_TEXT="docker-compose"
    return
  fi
  fail "no supported compose runtime found (podman-compose / docker compose / docker-compose)"
}

probe_once() {
  local headers
  local body
  local error_log
  local http_code
  local content_type
  local curl_status

  headers="$(mktemp)"
  body="$(mktemp)"
  error_log="$(mktemp)"

  set +e
  http_code="$(curl \
    --silent \
    --show-error \
    --max-time "${TIMEOUT}" \
    --dump-header "${headers}" \
    --output "${body}" \
    --write-out '%{http_code}' \
    "${URL}" 2>"${error_log}")"
  curl_status=$?
  set -e

  if (( curl_status != 0 )); then
    log "File Browser backend probe failed: $(tr '\n' ' ' < "${error_log}")"
    rm -f "${headers}" "${body}" "${error_log}"
    return 1
  fi

  content_type="$(awk 'BEGIN { IGNORECASE = 1 } /^content-type:/ { print $0 }' "${headers}" | tail -n 1)"
  if [[ "${http_code}" == "200" ]] && [[ "${content_type}" == *"text/html"* ]]; then
    rm -f "${headers}" "${body}" "${error_log}"
    return 0
  fi

  log "File Browser backend probe returned HTTP ${http_code} (${content_type:-no content-type})"
  rm -f "${headers}" "${body}" "${error_log}"
  return 1
}

wait_for_backend() {
  local attempt

  for (( attempt = 1; attempt <= WAIT_ATTEMPTS; attempt++ )); do
    if probe_once; then
      log "File Browser backend is healthy at ${URL}"
      return 0
    fi

    if (( attempt < WAIT_ATTEMPTS )); then
      sleep "${WAIT_SLEEP}"
    fi
  done

  return 1
}

restart_backend() {
  (( RESTART == 1 )) || return 1
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "run as root (sudo) to restart the File Browser backend"
  [[ -f "${ENV_FILE}" ]] || fail "env file not found: ${ENV_FILE}"
  [[ -f "${COMPOSE_FILE}" ]] || fail "compose file not found: ${COMPOSE_FILE}"

  set_compose_command
  log "starting File Browser backend with ${COMPOSE_CMD_TEXT}"
  "${COMPOSE_CMD[@]}" \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" \
    up -d
}

main() {
  parse_args "$@"
  command_exists curl || fail "curl not found"
  load_env_if_needed

  if wait_for_backend; then
    exit 0
  fi

  restart_backend
  WAIT_ATTEMPTS=6
  wait_for_backend || fail "File Browser backend is still unhealthy after compose start"
}

main "$@"
