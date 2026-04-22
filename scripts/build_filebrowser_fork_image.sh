#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DEFAULT_WORKSPACE="${REPO_ROOT}/vendor/filebrowser-upstream"
DEFAULT_IMAGE="localhost/filebrowser-snowbridge:dirsize"
WORKSPACE="${DEFAULT_WORKSPACE}"
IMAGE="${DEFAULT_IMAGE}"
CONTAINER_TOOL=""
PUSH_IMAGE=0
PULL_BASE=0
NO_CACHE=0
DRY_RUN=0
BUILD_BINARY=1
VERSION_OVERRIDE=""
COMMIT_OVERRIDE=""
STAGING_DIR=""
BINARY_SOURCE=""
WORKSPACE_VERSION=""
WORKSPACE_COMMIT=""

usage() {
  cat <<'EOF'
Usage: build_filebrowser_fork_image.sh [options]

Build and tag a local File Browser image from the patched upstream checkout.

Options:
  --workspace PATH            Fork workspace path. Default: vendor/filebrowser-upstream
  --image NAME[:TAG]          Image tag to build. Default: localhost/filebrowser-snowbridge:dirsize
  --container-tool TOOL       Build with `podman` or `docker`. Auto-detected when omitted.
  --skip-binary-build         Reuse an existing workspace `filebrowser` binary.
  --pull                      Ask the container builder to refresh base images.
  --no-cache                  Disable the container build cache.
  --push                      Push the final image tag after a successful build.
  --version STRING            Override the embedded File Browser version string.
  --commit SHA                Override the embedded commit SHA string.
  --dry-run                   Print commands without executing them.
  --help                      Show this help text.

Notes:
  - This helper expects the fork workspace to have already been prepared with
    `./scripts/setup_filebrowser_fork_workspace.sh` so `frontend/dist/` exists.
  - The helper stages a minimal container context instead of sending the full
    fork checkout, including `node_modules`, to the builder.

Typical flow:
  ./scripts/setup_filebrowser_fork_workspace.sh
  ./scripts/build_filebrowser_fork_image.sh
  # then set FILEBROWSER_IMAGE=localhost/filebrowser-snowbridge:dirsize
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

run_in_dir() {
  local dir="$1"
  shift

  if (( DRY_RUN )); then
    printf '+ cd %q && ' "${dir}"
    printf '%q ' "$@"
    printf '\n'
    return 0
  fi

  (
    cd "${dir}"
    "$@"
  )
}

cleanup() {
  if [[ -n "${STAGING_DIR}" && -d "${STAGING_DIR}" ]]; then
    rm -rf "${STAGING_DIR}"
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --workspace)
        [[ $# -ge 2 ]] || fail "--workspace requires a path"
        WORKSPACE="$2"
        shift 2
        ;;
      --image)
        [[ $# -ge 2 ]] || fail "--image requires a tag"
        IMAGE="$2"
        shift 2
        ;;
      --container-tool)
        [[ $# -ge 2 ]] || fail "--container-tool requires podman or docker"
        CONTAINER_TOOL="$2"
        shift 2
        ;;
      --skip-binary-build)
        BUILD_BINARY=0
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
      --version)
        [[ $# -ge 2 ]] || fail "--version requires a value"
        VERSION_OVERRIDE="$2"
        shift 2
        ;;
      --commit)
        [[ $# -ge 2 ]] || fail "--commit requires a value"
        COMMIT_OVERRIDE="$2"
        shift 2
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

require_workspace() {
  [[ -d "${WORKSPACE}" ]] || fail "workspace does not exist: ${WORKSPACE}"
  [[ -d "${WORKSPACE}/.git" ]] || fail "workspace is not a git checkout: ${WORKSPACE}"
  [[ -f "${WORKSPACE}/go.mod" ]] || fail "missing go.mod in workspace: ${WORKSPACE}"
  [[ -f "${WORKSPACE}/Dockerfile" ]] || fail "missing Dockerfile in workspace: ${WORKSPACE}"
  [[ -f "${WORKSPACE}/frontend/dist/index.html" ]] || fail "missing frontend/dist/index.html; run ./scripts/setup_filebrowser_fork_workspace.sh first"
  [[ -d "${WORKSPACE}/docker/common" ]] || fail "missing docker/common in workspace: ${WORKSPACE}"
  [[ -d "${WORKSPACE}/docker/alpine" ]] || fail "missing docker/alpine in workspace: ${WORKSPACE}"
}

resolve_container_tool() {
  if [[ -n "${CONTAINER_TOOL}" ]]; then
    case "${CONTAINER_TOOL}" in
      podman|docker) ;;
      *)
        fail "unsupported container tool: ${CONTAINER_TOOL} (expected podman or docker)"
        ;;
    esac
    command_exists "${CONTAINER_TOOL}" || fail "required command not found: ${CONTAINER_TOOL}"
    return 0
  fi

  if command_exists podman; then
    CONTAINER_TOOL="podman"
    return 0
  fi

  if command_exists docker; then
    CONTAINER_TOOL="docker"
    return 0
  fi

  fail "no supported container tool found; install podman or docker"
}

resolve_workspace_metadata() {
  WORKSPACE_COMMIT="${COMMIT_OVERRIDE:-$(git -C "${WORKSPACE}" log -n 1 --format=%h)}"
  WORKSPACE_VERSION="${VERSION_OVERRIDE:-$(git -C "${WORKSPACE}" describe --tags --abbrev=0 --match='v*' 2>/dev/null || true)}"
  WORKSPACE_VERSION="${WORKSPACE_VERSION#v}"

  if [[ -z "${WORKSPACE_VERSION}" ]]; then
    WORKSPACE_VERSION="0.0.0+${WORKSPACE_COMMIT}"
  fi
}

build_binary() {
  local ldflags

  command_exists go || fail "required command not found: go"
  ldflags="-s -w -X github.com/filebrowser/filebrowser/v2/version.Version=${WORKSPACE_VERSION} -X github.com/filebrowser/filebrowser/v2/version.CommitSHA=${WORKSPACE_COMMIT}"
  BINARY_SOURCE="${STAGING_DIR}/filebrowser"

  log "build File Browser binary"
  # The runtime image uses BusyBox/musl, so the staged binary must be static.
  run_in_dir "${WORKSPACE}" env CGO_ENABLED=0 go build -ldflags "${ldflags}" -o "${BINARY_SOURCE}" .
}

prepare_staging_dir() {
  STAGING_DIR="$(mktemp -d)"
  run_cmd mkdir -p "${STAGING_DIR}/docker"
}

stage_context() {
  if [[ -z "${BINARY_SOURCE}" ]]; then
    BINARY_SOURCE="${WORKSPACE}/filebrowser"
  fi

  run_cmd cp "${WORKSPACE}/Dockerfile" "${STAGING_DIR}/Dockerfile"
  if [[ "${BINARY_SOURCE}" != "${STAGING_DIR}/filebrowser" ]]; then
    run_cmd cp "${BINARY_SOURCE}" "${STAGING_DIR}/filebrowser"
  fi
  run_cmd cp -a "${WORKSPACE}/docker/common" "${STAGING_DIR}/docker/common"
  run_cmd cp -a "${WORKSPACE}/docker/alpine" "${STAGING_DIR}/docker/alpine"
}

build_image() {
  local args=()

  args=("${CONTAINER_TOOL}" build)

  if (( PULL_BASE )); then
    args+=(--pull)
  fi

  if (( NO_CACHE )); then
    args+=(--no-cache)
  fi

  args+=(
    --file "${STAGING_DIR}/Dockerfile"
    --label "org.opencontainers.image.title=filebrowser-snowbridge"
    --label "org.opencontainers.image.version=${WORKSPACE_VERSION}"
    --label "org.opencontainers.image.revision=${WORKSPACE_COMMIT}"
    --tag "${IMAGE}"
    "${STAGING_DIR}"
  )

  log "build image with ${CONTAINER_TOOL}"
  run_cmd "${args[@]}"
}

push_image_if_requested() {
  if (( ! PUSH_IMAGE )); then
    return 0
  fi

  log "push image ${IMAGE}"
  run_cmd "${CONTAINER_TOOL}" push "${IMAGE}"
}

main() {
  trap cleanup EXIT
  parse_args "$@"
  require_workspace
  resolve_container_tool
  resolve_workspace_metadata
  prepare_staging_dir

  if (( BUILD_BINARY )); then
    build_binary
  else
    BINARY_SOURCE="${WORKSPACE}/filebrowser"
    [[ -f "${BINARY_SOURCE}" ]] || fail "workspace binary not found at ${BINARY_SOURCE}; omit --skip-binary-build or build it first"
  fi

  stage_context
  build_image
  push_image_if_requested

  log "image ready: ${IMAGE}"
  log "next step: set FILEBROWSER_IMAGE=${IMAGE} in config/web/filebrowser/filebrowser.env.local"
}

main "$@"
