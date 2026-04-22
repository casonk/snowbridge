#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

BUILD_SCRIPT="${REPO_ROOT}/scripts/build_filebrowser_fork_image.sh"
SETUP_WEB_SCRIPT="${REPO_ROOT}/scripts/setup_caddy_filebrowser.sh"
ENV_LOCAL_DEFAULT="${REPO_ROOT}/config/web/filebrowser/filebrowser.env.local"
PRIVATE_CADDY_LOCAL="${REPO_ROOT}/config/web/caddy/Caddyfile.private-vpn.local"
PRIVATE_MTLS_CADDY_LOCAL="${REPO_ROOT}/config/web/caddy/Caddyfile.private-vpn-mtls.local"
PUBLIC_CADDY_LOCAL="${REPO_ROOT}/config/web/caddy/Caddyfile.public.local"
PUBLIC_PRIVATE_IP_CADDY_LOCAL="${REPO_ROOT}/config/web/caddy/Caddyfile.public-private-ip.local"

DEFAULT_IMAGE="localhost/filebrowser-snowbridge:dirsize"
MODE="private-vpn"
ENV_FILE="${ENV_LOCAL_DEFAULT}"
IMAGE="${DEFAULT_IMAGE}"
CONTAINER_TOOL=""
SKIP_BUILD=0
PULL_BASE=0
NO_CACHE=0
PUSH_IMAGE=0
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: deploy_filebrowser_fork_image.sh [options]

Build the local File Browser fork image, write the chosen image tag into
config/web/filebrowser/filebrowser.env.local, and recreate the web stack.

Options:
  --image NAME[:TAG]          Image tag to deploy. Default: localhost/filebrowser-snowbridge:dirsize
  --mode private-vpn|private-vpn-mtls|public|public-private-ip
                              Caddy/File Browser web mode to recreate. Default: private-vpn
  --env-file PATH             Env file to update. Default: config/web/filebrowser/filebrowser.env.local
  --container-tool TOOL       Build image with `podman` or `docker`. Auto-detected when omitted.
  --skip-build                Reuse an already-built image tag and only update the env file + recreate.
  --pull                      Ask the container builder to refresh base images.
  --no-cache                  Disable the container build cache.
  --push                      Push the image after building it.
  --dry-run                   Print commands without executing them.
  --help                      Show this help text.

Notes:
  - If the local web env or mode-specific Caddyfile is missing, this helper
    will initialize them first via `setup_caddy_filebrowser.sh --init-local-configs`.
  - The actual image build and stack recreation run through `sudo` when needed
    so the built image is visible to the same root-owned runtime context used
    by `setup_caddy_filebrowser.sh`.

Typical flow:
  ./scripts/setup_filebrowser_fork_workspace.sh
  ./scripts/deploy_filebrowser_fork_image.sh
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

print_cmd() {
  printf '+ '
  printf '%q ' "$@"
  printf '\n'
}

run_cmd() {
  if (( DRY_RUN )); then
    print_cmd "$@"
    return 0
  fi
  "$@"
}

run_privileged_cmd() {
  if (( DRY_RUN )); then
    if [[ "${EUID}" -eq 0 ]]; then
      print_cmd "$@"
    else
      print_cmd sudo "$@"
    fi
    return 0
  fi

  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
    return 0
  fi

  command_exists sudo || fail "sudo is required for image build and stack recreation"
  sudo "$@"
}

ensure_mode() {
  case "${MODE}" in
    private-vpn|private-vpn-mtls|public|public-private-ip) ;;
    *)
      fail "invalid mode: ${MODE}"
      ;;
  esac
}

selected_caddy_local() {
  case "${MODE}" in
    private-vpn)
      printf '%s\n' "${PRIVATE_CADDY_LOCAL}"
      ;;
    private-vpn-mtls)
      printf '%s\n' "${PRIVATE_MTLS_CADDY_LOCAL}"
      ;;
    public)
      printf '%s\n' "${PUBLIC_CADDY_LOCAL}"
      ;;
    public-private-ip)
      printf '%s\n' "${PUBLIC_PRIVATE_IP_CADDY_LOCAL}"
      ;;
    *)
      fail "invalid mode: ${MODE}"
      ;;
  esac
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --image)
        [[ $# -ge 2 ]] || fail "--image requires a tag"
        IMAGE="$2"
        shift 2
        ;;
      --mode)
        [[ $# -ge 2 ]] || fail "--mode requires a value"
        MODE="$2"
        shift 2
        ;;
      --env-file)
        [[ $# -ge 2 ]] || fail "--env-file requires a path"
        ENV_FILE="$2"
        shift 2
        ;;
      --container-tool)
        [[ $# -ge 2 ]] || fail "--container-tool requires podman or docker"
        CONTAINER_TOOL="$2"
        shift 2
        ;;
      --skip-build)
        SKIP_BUILD=1
        shift
        ;;
      --pull)
        PULL_BASE=1
        shift
        ;;
      --no-cache)
        NO_CACHE=1
        shift
        ;;
      --push)
        PUSH_IMAGE=1
        shift
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --help)
        usage
        exit 0
        ;;
      *)
        fail "unknown option: $1"
        ;;
    esac
  done
}

ensure_scripts_exist() {
  [[ -x "${BUILD_SCRIPT}" ]] || fail "build helper not found or not executable: ${BUILD_SCRIPT}"
  [[ -x "${SETUP_WEB_SCRIPT}" ]] || fail "web setup helper not found or not executable: ${SETUP_WEB_SCRIPT}"
}

ensure_local_web_configs() {
  local caddy_local
  caddy_local="$(selected_caddy_local)"

  if [[ -f "${ENV_FILE}" && -f "${caddy_local}" ]]; then
    return 0
  fi

  log "initialize missing local web config files for ${MODE}"
  run_cmd "${SETUP_WEB_SCRIPT}" --init-local-configs --mode "${MODE}" --env-file "${ENV_FILE}"
}

replace_env_setting() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp

  if [[ ! -f "${file}" ]]; then
    if (( DRY_RUN )); then
      return 0
    fi
    fail "env file not found: ${file}"
  fi

  if (( DRY_RUN )); then
    return 0
  fi

  if [[ ! -r "${file}" || ! -w "${file}" ]]; then
    run_privileged_cmd /bin/bash -c '
      file="$1"
      key="$2"
      value="$3"
      tmp="$(mktemp)"
      awk -v key="${key}" -v value="${value}" "
        BEGIN { updated = 0 }
        index(\$0, key \"=\") == 1 {
          print key \"=\" value
          updated = 1
          next
        }
        { print }
        END {
          if (!updated) {
            print key \"=\" value
          }
        }
      " "${file}" > "${tmp}"
      install -m "$(stat -c "%a" "${file}")" -o "$(stat -c "%u" "${file}")" -g "$(stat -c "%g" "${file}")" "${tmp}" "${file}"
      rm -f "${tmp}"
    ' _ "${file}" "${key}" "${value}"
    return 0
  fi

  tmp="$(mktemp)"
  awk -v key="${key}" -v value="${value}" '
    BEGIN { updated = 0 }
    index($0, key "=") == 1 {
      print key "=" value
      updated = 1
      next
    }
    { print }
    END {
      if (!updated) {
        print key "=" value
      }
    }
  ' "${file}" > "${tmp}"
  cat "${tmp}" > "${file}"
  rm -f "${tmp}"
}

build_image_if_needed() {
  local args=()

  if (( SKIP_BUILD )); then
    return 0
  fi

  args=("${BUILD_SCRIPT}" --image "${IMAGE}")

  if [[ -n "${CONTAINER_TOOL}" ]]; then
    args+=(--container-tool "${CONTAINER_TOOL}")
  fi
  if (( PULL_BASE )); then
    args+=(--pull)
  fi
  if (( NO_CACHE )); then
    args+=(--no-cache)
  fi
  if (( PUSH_IMAGE )); then
    args+=(--push)
  fi
  if (( DRY_RUN )); then
    args+=(--dry-run)
  fi

  run_privileged_cmd "${args[@]}"
}

recreate_stack() {
  local args=()

  args=("${SETUP_WEB_SCRIPT}" --mode "${MODE}" --env-file "${ENV_FILE}" --recreate)
  run_privileged_cmd "${args[@]}"
}

main() {
  parse_args "$@"
  ensure_mode
  ensure_scripts_exist
  ensure_local_web_configs
  replace_env_setting "${ENV_FILE}" "FILEBROWSER_IMAGE" "${IMAGE}"
  if (( DRY_RUN )); then
    log "planned FILEBROWSER_IMAGE=${IMAGE} update in ${ENV_FILE}"
  else
    log "set FILEBROWSER_IMAGE=${IMAGE} in ${ENV_FILE}"
  fi
  build_image_if_needed
  recreate_stack
  if (( DRY_RUN )); then
    log "would deploy ${IMAGE} for mode ${MODE}"
  else
    log "deployed ${IMAGE} for mode ${MODE}"
  fi
}

main "$@"
