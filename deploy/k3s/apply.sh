#!/usr/bin/env bash
# apply.sh — create the namespace, then the volumes, the network policy and the workload, in that order.
# Machine-specific values (paths, node, uid, SELinux level) are rendered here and never committed.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s.yaml}"
CONFIG_FILE="${SNOWBRIDGE_FILES_DEPLOY_CONFIG:-$REPO/config/deploy.local.env}"
NS=snowbridge-files

# values: POD_UID, POD_GID, SELINUX_LEVEL (the checks in prepare-host.sh run first; a failure stops here)
eval "$("$HERE/prepare-host.sh" --print-env)"
node="$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')"
[ -n "$node" ] || { echo "no node found" >&2; exit 1; }

kubectl apply -f "$HERE/manifests/00-namespace.yaml"
python3 -I "$HERE/render_volumes.py" volumes "$node" | kubectl apply -f -

# Icon switches come from the gitignored config file; absent values leave the feature off.
cm=(kubectl -n "$NS" create configmap snowbridge-files-env --dry-run=client -o yaml)
if [ -f "$CONFIG_FILE" ]; then
  for pair in AI_FOLDER_ICONS AI_FOLDER_ICONS_MODEL AI_FOLDER_ICONS_PREFETCH_USER; do
    line="$(python3 -I "$HERE/env_value.py" "$CONFIG_FILE" "SNOWBRIDGE_FILES_$pair")"
    [ -n "$line" ] && cm+=("--from-literal=FB_$pair=${line#*=}")
  done
fi
"${cm[@]}" | kubectl apply -f -

kubectl apply -f "$HERE/manifests/30-network-policy.yaml"
python3 -I "$HERE/render_volumes.py" workload <"$HERE/manifests/20-deployment.yaml" \
  | sed -e "s/@@UID@@/$POD_UID/" -e "s/@@GID@@/$POD_GID/" -e "s/@@SELINUX_LEVEL@@/$SELINUX_LEVEL/" \
  | kubectl apply -f -
kubectl -n "$NS" rollout restart deploy/snowbridge-files-web >/dev/null
kubectl -n "$NS" rollout status deploy/snowbridge-files-web --timeout=180s
