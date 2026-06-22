#!/usr/bin/env bash
# repair_share_access.sh — one-command Snowbridge share repair with debug capture

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "error: run as root (sudo)" >&2
    exit 1
fi

on_error() {
    local report
    report="${REPO_ROOT}/reports/private-access-debug-$(date +%Y%m%d-%H%M%S).log"
    echo "==> Snowbridge repair failed; capturing debug report…" >&2
    if bash "${REPO_ROOT}/scripts/debug_private_access.sh" --output "${report}"; then
        echo "==> Debug report: ${report}" >&2
    else
        echo "warning: debug report capture also failed" >&2
    fi
}

trap on_error ERR

echo "==> Repairing Snowbridge share access…"
bash "${REPO_ROOT}/scripts/start_snowbridge.sh"
echo "==> Snowbridge share repair complete."
