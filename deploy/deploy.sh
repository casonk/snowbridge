#!/usr/bin/env bash
# deploy.sh — run snowbridge-files the way you choose: dev, host or k3s. One entry point, one setting.
#
#   deploy/deploy.sh [--mode dev|host|k3s] up|down|status|verify|mode
#
# The mode comes from, first match wins:
#   1. --mode on the command line
#   2. SNOWBRIDGE_FILES_DEPLOY_MODE in the environment
#   3. SNOWBRIDGE_FILES_DEPLOY_MODE in config/deploy.local.env (copy config/deploy.example.env; never committed)
#   4. dev
#
# Only k3s mode touches kubectl, podman or a cluster. See docs/deployment.md.
# -E: the ERR trap used by `k3s up` must also fire for failures inside functions.
set -eEuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
K3S_DIR="${SNOWBRIDGE_FILES_K3S_DIR:-$HERE/k3s}"
CONFIG_FILE="${SNOWBRIDGE_FILES_DEPLOY_CONFIG:-$REPO/config/deploy.local.env}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/snowbridge-files"
MARKER="$STATE_DIR/deploy-mode"
MODES="dev host k3s"
UNIT="snowbridge-filebrowser.service"
BRIDGE="snowbridge-files-proxy.socket"
NAMESPACE="snowbridge-files"
PORT="${SNOWBRIDGE_FILES_PORT:-8080}"

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

mode="" source_of=""
cmd=""
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) mode="${2:?--mode needs dev, host or k3s}"; source_of="--mode"; shift 2 ;;
    --mode=*) mode="${1#--mode=}"; source_of="--mode"; shift ;;
    -h|--help) usage 0 ;;
    up|down|status|verify|mode) cmd="$1"; shift ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
done
[ -n "$cmd" ] || usage 2

if [ -z "$mode" ] && [ -n "${SNOWBRIDGE_FILES_DEPLOY_MODE:-}" ]; then mode="$SNOWBRIDGE_FILES_DEPLOY_MODE"; source_of="environment"; fi
if [ -z "$mode" ] && [ -f "$CONFIG_FILE" ]; then
  # No `|| true`: if reading the config fails, say so rather than quietly choosing dev.
  line="$(python3 -I "$K3S_DIR/env_value.py" "$CONFIG_FILE" SNOWBRIDGE_FILES_DEPLOY_MODE)"
  if [ -n "$line" ]; then mode="${line#SNOWBRIDGE_FILES_DEPLOY_MODE=}"; source_of="$CONFIG_FILE"; fi
fi
if [ -z "$mode" ]; then mode="dev"; source_of="default"; fi
case " $MODES " in *" $mode "*) ;; *) echo "unknown deployment mode '$mode' (from $source_of); use one of: $MODES" >&2; exit 2 ;; esac

need() { command -v "$1" >/dev/null 2>&1 || { echo "$2" >&2; exit 1; }; }
marker_is_k3s() { [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = k3s ]; }
set_marker() { mkdir -p "$STATE_DIR"; echo "$1" > "$MARKER"; }
clear_marker() { rm -f "$MARKER"; }
kc() { KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s.yaml}" "${KUBECTL:-kubectl}" "$@"; }
registry_ready() { curl -fsS --max-time 5 http://10.43.0.50:5000/v2/ >/dev/null 2>&1; }
require_cluster_tools() {
  need "${KUBECTL:-kubectl}" "k3s mode needs kubectl and a running K3s (see docs/deployment.md). dev and host modes need neither."
  need podman "k3s mode needs podman, to build the image"
  export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s.yaml}"
  registry_ready || {
    echo "k3s mode needs the in-cluster registry: run install-registry.sh and enable-registry-mirror.sh from crew-chief's deploy/k3s" >&2
    exit 1
  }
}

unit_installed() { systemctl --user cat "$UNIT" >/dev/null 2>&1; }
bridge_on() { command -v systemctl >/dev/null 2>&1 && systemctl --user is-active --quiet "$BRIDGE" 2>/dev/null; }
healthz() { curl -fsS --max-time 5 "http://127.0.0.1:$PORT/health" && echo; }
show_units() {
  for u in "$UNIT" "$BRIDGE"; do
    printf '%-30s %-9s %s\n' "$u" "$(systemctl --user is-active "$u" 2>/dev/null || true)" "$(systemctl --user is-enabled "$u" 2>/dev/null || true)"
  done
}

case "$cmd:$mode" in
  mode:*) echo "$mode (from $source_of)" ;;

  # ---- dev: the service in the foreground on 127.0.0.1, no systemd, no cluster ----
  up:dev) bin="${SNOWBRIDGE_FILES_FORK_DIR:-$REPO/vendor/filebrowser-upstream}/filebrowser"
    [ -x "$bin" ] || { echo "build the fork first: (cd vendor/filebrowser-upstream && go build -o filebrowser .)" >&2; exit 1; }
    scratch="${SNOWBRIDGE_FILES_DEV_DIR:-${TMPDIR:-/tmp}/snowbridge-files-dev}"; mkdir -p "$scratch/root"
    # A scratch database and an empty root: dev mode never touches the real share or database.
    exec "$bin" -a 127.0.0.1 -p "$PORT" -r "$scratch/root" -d "$scratch/filebrowser.db" ;;
  down:dev) echo "dev mode runs in the foreground; stop it with Ctrl-C" ;;
  status:dev|verify:dev) healthz || echo "not answering on 127.0.0.1:$PORT (dev mode runs in the foreground: deploy/deploy.sh up)" ;;

  # ---- host: the existing rootful container, started and stopped by a user unit through a scoped sudo rule
  #      (deploy/host/install-host-unit.sh installs both); it listens on 127.0.0.1:$PORT ----
  up:host)
    if bridge_on; then
      echo "the K3s bridge is on; switching back to the host service"
      "$K3S_DIR/bridge.sh" off
    else
      unit_installed || { echo "$UNIT is not installed (see docs/deployment.md)" >&2; exit 1; }
      systemctl --user enable --now "$UNIT"
    fi
    clear_marker
    ;;
  down:host) systemctl --user disable --now "$UNIT"; clear_marker ;;
  status:host) show_units ;;
  verify:host) healthz ;;

  # ---- k3s: a pod behind a loopback forwarder, so the front end keeps using 127.0.0.1:$PORT ----
  up:k3s)
    require_cluster_tools
    "$K3S_DIR/prepare-host.sh"
    "$K3S_DIR/build-image.sh"
    # The database has one owner at a time: release it from the host service before the pod starts,
    # and put the host service back if anything below fails.
    if unit_installed && systemctl --user is-active --quiet "$UNIT"; then
      systemctl --user stop "$UNIT"
      trap '"$K3S_DIR/bridge.sh" off || true' ERR
    fi
    "$K3S_DIR/apply.sh"
    "$K3S_DIR/verify.sh"
    # `bridge on` stops and disables the host service; if it cannot come up, put the host service back.
    trap '"$K3S_DIR/bridge.sh" off || true' ERR
    "$K3S_DIR/bridge.sh" on
    trap - ERR
    set_marker k3s
    ;;
  down:k3s)
    # The pod holds the database lock, so stop it (keeping the manifests) before the host container starts.
    kc -n "$NAMESPACE" scale deploy/snowbridge-files-web --replicas=0
    kc -n "$NAMESPACE" wait --for=delete pod --all --timeout=60s || true
    "$0" --mode host up
    echo "rolled back to the host service; the pod is scaled to 0 (kubectl delete ns $NAMESPACE removes it)" ;;
  status:k3s) show_units; kc -n "$NAMESPACE" get pods 2>&1 || true ;;
  verify:k3s) "$K3S_DIR/verify.sh" ;;
esac
