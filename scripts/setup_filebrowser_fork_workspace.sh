#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DEFAULT_WORKSPACE="${REPO_ROOT}/vendor/filebrowser-upstream"
WORKSPACE="${DEFAULT_WORKSPACE}"
INSTALL_OS_PACKAGES=0
INSTALL_ONLY=0
SKIP_BACKEND_DEPS=0
SKIP_BACKEND_TEST=0
SKIP_FRONTEND_INSTALL=0
SKIP_FRONTEND_LINT=0
SKIP_FRONTEND_TEST=0
SKIP_FRONTEND_BUILD=0
DRY_RUN=0
PNPM_VERSION="10.33.0"
NODE_CMD=()
NPM_CMD=()
NPX_CMD=()
PNPM_CMD=()
TOOL_BIN_DIR=""
PATH_PREFIX=""

usage() {
  cat <<'EOF'
Usage: setup_filebrowser_fork_workspace.sh [options]

Install prerequisites and run the local frontend/backend checks for the
File Browser fork workspace.

Options:
  --workspace PATH            Fork workspace path. Default: vendor/filebrowser-upstream
  --install-os-packages       Install OS packages for Go, Node, npm, and a C compiler
                              when a supported package manager is available.
  --install-only              Install project dependencies only; skip tests, lint, and build.
  --skip-backend-deps         Skip `go mod download`.
  --skip-backend-test         Skip `go test --race ./...`.
  --skip-frontend-install     Skip `pnpm install --frozen-lockfile`.
  --skip-frontend-lint        Skip `pnpm run lint`.
  --skip-frontend-test        Skip `pnpm run test`.
  --skip-frontend-build       Skip `pnpm run build`.
  --dry-run                   Print commands without executing them.
  --help                      Show this help text.

Notes:
  - Upstream currently expects Go >= 1.25 (CI uses 1.26.x), Node >= 24,
    and pnpm >= 10.
  - If pnpm is missing, this script will prefer Corepack and otherwise fall
    back to `npx --yes pnpm@10.33.0`.

Typical flow:
  ./scripts/setup_filebrowser_fork_workspace.sh --install-os-packages
  ./scripts/setup_filebrowser_fork_workspace.sh
  ./scripts/setup_filebrowser_fork_workspace.sh --install-only
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
  if [[ -n "${PATH_PREFIX}" ]]; then
    PATH="${PATH_PREFIX}:${PATH}" "$@"
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
    if [[ -n "${PATH_PREFIX}" ]]; then
      PATH="${PATH_PREFIX}:${PATH}" "$@"
    else
      "$@"
    fi
  )
}

run_privileged() {
  if [[ "${EUID}" -eq 0 ]]; then
    run_cmd "$@"
    return 0
  fi

  command_exists sudo || fail "sudo is required for --install-os-packages when not already root"
  run_cmd sudo "$@"
}

require_workspace() {
  [[ -d "${WORKSPACE}" ]] || fail "workspace does not exist: ${WORKSPACE}"
  [[ -f "${WORKSPACE}/go.mod" ]] || fail "missing go.mod in workspace: ${WORKSPACE}"
  [[ -f "${WORKSPACE}/frontend/package.json" ]] || fail "missing frontend/package.json in workspace: ${WORKSPACE}"
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
  fail "no supported package manager found (supported: dnf, apt-get, yum)"
}

install_prerequisites() {
  local package_manager
  package_manager="$(find_package_manager)"

  case "${package_manager}" in
    dnf)
      log "installing File Browser fork prerequisites with dnf"
      run_privileged dnf install -y golang gcc nodejs24 nodejs24-npm
      ;;
    apt-get)
      log "refreshing apt package metadata"
      run_privileged apt-get update
      log "installing File Browser fork prerequisites with apt-get"
      run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y \
        golang-go nodejs npm build-essential
      ;;
    yum)
      log "installing File Browser fork prerequisites with yum"
      run_privileged yum install -y golang gcc nodejs24 nodejs24-npm
      ;;
    *)
      fail "unsupported package manager: ${package_manager}"
      ;;
  esac
}

require_command() {
  command_exists "$1" || fail "required command not found: $1"
}

node_major() {
  local version
  version="$("$@" --version 2>/dev/null || true)"
  version="${version#v}"
  printf '%s\n' "${version%%.*}"
}

go_version() {
  local raw
  raw="$(go version 2>/dev/null || true)"
  raw="${raw#* go}"
  printf '%s\n' "${raw%% *}"
}

prepare_tool_shims() {
  TOOL_BIN_DIR="$(mktemp -d)"
  ln -sf "$(command -v "${NODE_CMD[0]}")" "${TOOL_BIN_DIR}/node"
  ln -sf "$(command -v "${NPM_CMD[0]}")" "${TOOL_BIN_DIR}/npm"
  if (( ${#NPX_CMD[@]} > 0 )); then
    ln -sf "$(command -v "${NPX_CMD[0]}")" "${TOOL_BIN_DIR}/npx"
  fi
  PATH_PREFIX="${TOOL_BIN_DIR}"
}

cleanup() {
  if [[ -n "${TOOL_BIN_DIR}" && -d "${TOOL_BIN_DIR}" ]]; then
    rm -rf "${TOOL_BIN_DIR}"
  fi
}

current_node_version() {
  if [[ -n "${PATH_PREFIX}" ]]; then
    PATH="${PATH_PREFIX}:${PATH}" node --version
    return 0
  fi
  node --version
}

pnpm_major() {
  local version
  version="$1"
  version="${version#v}"
  printf '%s\n' "${version%%.*}"
}

require_node_version() {
  local major
  major="$(node_major "${NODE_CMD[@]}")"
  [[ -n "${major}" ]] || fail "unable to determine Node.js version"
  (( major >= 24 )) || fail "Node.js ${major} detected; upstream requires Node.js >= 24"
}

require_go_version() {
  local version major remainder minor
  version="$(go_version)"
  [[ -n "${version}" ]] || fail "unable to determine Go version"

  major="${version%%.*}"
  remainder="${version#*.}"
  minor="${remainder%%.*}"

  if (( major < 1 || (major == 1 && minor < 25) )); then
    fail "Go ${version} detected; upstream requires Go >= 1.25 and CI uses 1.26.x"
  fi

  if (( major == 1 && minor < 26 )); then
    warn "Go ${version} meets go.mod but upstream CI currently runs 1.26.x"
  fi
}

resolve_pnpm_command() {
  local detected_version detected_major

  if command_exists pnpm; then
    if [[ -n "${PATH_PREFIX}" ]]; then
      detected_version="$(PATH="${PATH_PREFIX}:${PATH}" pnpm --version)"
    else
      detected_version="$(pnpm --version)"
    fi
    detected_major="$(pnpm_major "${detected_version}")"
    if [[ -n "${detected_major}" ]] && (( detected_major >= 10 )); then
      PNPM_CMD=(pnpm)
      return 0
    fi
    warn "pnpm ${detected_version} is older than the upstream requirement; trying a managed pnpm 10 toolchain"
  fi

  if command_exists corepack; then
    log "activating pnpm ${PNPM_VERSION} via corepack"
    run_cmd corepack enable
    run_cmd corepack prepare "pnpm@${PNPM_VERSION}" --activate
    PNPM_CMD=(pnpm)
    return 0
  fi

  if (( ${#NPX_CMD[@]} > 0 )); then
    warn "pnpm/corepack not found; using npx pnpm@${PNPM_VERSION}"
    PNPM_CMD=(npx --yes "pnpm@${PNPM_VERSION}")
    return 0
  fi

  fail "pnpm is unavailable and no corepack or npx fallback was found"
}

resolve_node_toolchain() {
  if command_exists node; then
    NODE_CMD=(node)
    NPM_CMD=(npm)
    NPX_CMD=(npx)
    if (( "$(node_major "${NODE_CMD[@]}")" >= 24 )); then
      return 0
    fi
  fi

  if command_exists node-24; then
    NODE_CMD=(node-24)
    command_exists npm-24 || fail "node-24 is installed but npm-24 is missing; install nodejs24-npm"
    NPM_CMD=(npm-24)
    if command_exists npx-24; then
      NPX_CMD=(npx-24)
    else
      NPX_CMD=()
    fi
    prepare_tool_shims
    return 0
  fi

  if [[ -n "${NODE_CMD[*]:-}" ]]; then
    fail "Node.js $(node_major "${NODE_CMD[@]}") detected; upstream requires Node.js >= 24"
  fi

  fail "required command not found: node (or Fedora's node-24)"
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --workspace)
        [[ $# -ge 2 ]] || fail "--workspace requires a path"
        WORKSPACE="$2"
        shift 2
        ;;
      --install-os-packages)
        INSTALL_OS_PACKAGES=1
        shift
        ;;
      --install-only)
        INSTALL_ONLY=1
        shift
        ;;
      --skip-backend-deps)
        SKIP_BACKEND_DEPS=1
        shift
        ;;
      --skip-backend-test)
        SKIP_BACKEND_TEST=1
        shift
        ;;
      --skip-frontend-install)
        SKIP_FRONTEND_INSTALL=1
        shift
        ;;
      --skip-frontend-lint)
        SKIP_FRONTEND_LINT=1
        shift
        ;;
      --skip-frontend-test)
        SKIP_FRONTEND_TEST=1
        shift
        ;;
      --skip-frontend-build)
        SKIP_FRONTEND_BUILD=1
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

main() {
  trap cleanup EXIT
  parse_args "$@"
  require_workspace

  if (( INSTALL_OS_PACKAGES )); then
    install_prerequisites
  fi

  require_command go
  resolve_node_toolchain
  require_go_version
  require_node_version
  resolve_pnpm_command

  log "workspace: ${WORKSPACE}"
  log "go version: $(go version)"
  log "node version: $(current_node_version)"

  if (( ! SKIP_BACKEND_DEPS )); then
    log "download Go module dependencies"
    run_in_dir "${WORKSPACE}" go mod download
  fi

  if (( ! SKIP_FRONTEND_INSTALL )); then
    log "install frontend dependencies"
    run_in_dir "${WORKSPACE}/frontend" "${PNPM_CMD[@]}" install --frozen-lockfile
  fi

  if (( INSTALL_ONLY )); then
    log "install-only mode completed"
    return 0
  fi

  if (( ! SKIP_BACKEND_TEST )); then
    log "run backend tests"
    run_in_dir "${WORKSPACE}" go test --race ./...
  fi

  if (( ! SKIP_FRONTEND_LINT )); then
    log "run frontend lint"
    run_in_dir "${WORKSPACE}/frontend" "${PNPM_CMD[@]}" run lint
  fi

  if (( ! SKIP_FRONTEND_TEST )); then
    log "run frontend tests"
    run_in_dir "${WORKSPACE}/frontend" "${PNPM_CMD[@]}" run test
  fi

  if (( ! SKIP_FRONTEND_BUILD )); then
    log "run frontend build"
    run_in_dir "${WORKSPACE}/frontend" "${PNPM_CMD[@]}" run build
  fi

  log "fork workspace bootstrap and validation completed"
}

main "$@"
