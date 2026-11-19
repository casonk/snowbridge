#!/usr/bin/env bash
# verify.sh — prove the pod is healthy and isolated. A check that cannot run is a FAIL, never an ok.
#   exit 0 all checks passed   1 a check failed   2 nothing could be verified (no pod, no cluster)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s.yaml}"
NS=snowbridge-files
CLUSTER_IP=10.43.0.64
PORT=8080
fails=0
ok() { echo "ok    $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }
warn() { echo "warn  $*"; }

kubectl version --request-timeout=10s >/dev/null 2>&1 || { echo "nothing was verified: the cluster does not answer" >&2; exit 2; }
pod="$(kubectl -n "$NS" get pods -l app=snowbridge-files-web -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
[ -n "$pod" ] || { echo "nothing was verified: no pod in namespace $NS" >&2; exit 2; }
ready="$(kubectl -n "$NS" get pod "$pod" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null)"
[ "$ready" = true ] || { echo "nothing was verified: pod $pod is not ready" >&2; exit 2; }

# 1. the node reaches the service (this is the path the forwarder uses)
curl -fsS --max-time 5 "http://$CLUSTER_IP:$PORT/health" >/dev/null && ok "node reaches the service" || fail "node cannot reach http://$CLUSTER_IP:$PORT/health"

# 2. a burst: a real page load is many requests at once, and one request passing proved nothing before
burst_fail=0
for _ in $(seq 1 40); do
  { curl -fsS --max-time 10 "http://$CLUSTER_IP:$PORT/health" >/dev/null || echo x >>"${TMPDIR:-/tmp}/sb-burst.$$"; } &
done
wait
[ -e "${TMPDIR:-/tmp}/sb-burst.$$" ] && burst_fail=$(wc -l <"${TMPDIR:-/tmp}/sb-burst.$$") && rm -f "${TMPDIR:-/tmp}/sb-burst.$$"
[ "$burst_fail" -eq 0 ] && ok "40 concurrent requests answered" || fail "$burst_fail of 40 concurrent requests failed"

# 3. every mounted share folder shows real content, not an empty mountpoint
while IFS= read -r mount; do
  count="$(kubectl -n "$NS" exec "$pod" -- sh -c "ls -A '$mount' | head -1 | wc -l" 2>/dev/null || echo err)"
  case "$count" in
    1) ok "$mount has content" ;;
    0) fail "$mount is empty in the pod (not mounted, or the source folder is empty)" ;;
    *) fail "$mount could not be read in the pod" ;;
  esac
done < <(python3 -I "$HERE/render_volumes.py" mounts)

# 4. the database is writable, the config is not
kubectl -n "$NS" exec "$pod" -- sh -c 'touch /database/.verify && rm /database/.verify' >/dev/null 2>&1 \
  && ok "database writable" || fail "database not writable"
kubectl -n "$NS" exec "$pod" -- sh -c 'touch /config/.verify' >/dev/null 2>&1 \
  && fail "config is writable (it must be read-only)" || ok "config read-only"

# 5. no outbound network from the pod
kubectl -n "$NS" exec "$pod" -- sh -c 'wget -T 4 -q -O- http://1.1.1.1/ >/dev/null 2>&1' \
  && fail "the pod reached the internet" || ok "pod has no internet"

# 6. another namespace cannot reach the service (it must time out)
kubectl -n default delete pod sb-verify-xns --ignore-not-found --wait=true >/dev/null 2>&1
kubectl -n default run sb-verify-xns --image=localhost:5000/snowbridge-files:k3s --restart=Never \
  --command -- wget -T 5 -q -O- "http://$CLUSTER_IP:$PORT/health" >/dev/null 2>&1
phase=""
for _ in $(seq 1 30); do
  phase="$(kubectl -n default get pod sb-verify-xns -o jsonpath='{.status.phase}' 2>/dev/null)"
  case "$phase" in Succeeded|Failed) break ;; esac
  sleep 2
done
kubectl -n default delete pod sb-verify-xns --ignore-not-found --wait=true >/dev/null 2>&1
case "$phase" in
  Failed) ok "another namespace cannot reach the service" ;;
  Succeeded) fail "another namespace reached the service" ;;
  *) fail "the cross-namespace probe did not finish (phase '${phase:-none}')" ;;
esac

# 7. best effort: the local-model socket for folder icons (a network policy does not cover unix sockets)
kubectl -n "$NS" exec "$pod" -- sh -c 'test -S /config/crew-chief.sock' >/dev/null 2>&1 \
  && ok "model socket present" || warn "model socket not present: folder icons will not generate new suggestions in the pod"

[ "$fails" -eq 0 ] && { echo "all checks passed"; exit 0; }
echo "$fails check(s) failed" >&2
exit 1
