#!/usr/bin/env bash
# start_snowbridge.sh — bring up the snowbridge share after LUKS volumes are mounted
#
# Append a call to this script at the end of your LUKS bootstrap so the share
# is ready as soon as the drives are decrypted.
#
# Usage:
#   sudo bash scripts/start_snowbridge.sh
#
# What it does:
#   1. Refreshes fstab bind mounts whose sources are on LUKS ext4 drives.
#   2. Starts (or ensures) the Samba daemons for iOS Files app access.
#   3. Brings up the File Browser + Caddy container stack.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/config/web/filebrowser/docker-compose.local.yml"
ENV_FILE="${REPO_ROOT}/config/web/filebrowser/filebrowser.env.local"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "error: run as root (sudo)" >&2
    exit 1
fi

# ── 1. Refresh LUKS bind mounts ─────────────────────────────────────────────
echo "==> Refreshing LUKS bind mounts…"
bash "${REPO_ROOT}/scripts/remount_luks_share.sh"

# ── 2. WireGuard ─────────────────────────────────────────────────────────────
echo "==> Starting WireGuard…"
if [[ -f /etc/wireguard/wg0.conf ]]; then
    systemctl start wg-quick@wg0
    echo "    wg0: $(systemctl is-active wg-quick@wg0)"
    # dnsmasq binds 10.99.0.1 which only exists after wg0 is up; restart it now
    if systemctl is-enabled dnsmasq &>/dev/null; then
        systemctl restart dnsmasq
        echo "    dnsmasq: $(systemctl is-active dnsmasq)"
    fi
else
    echo "    warning: /etc/wireguard/wg0.conf not found, skipping"
fi

# ── 3. NordVPN ───────────────────────────────────────────────────────────────
echo "==> Starting NordVPN…"
if command -v nordvpn &>/dev/null; then
    # Ensure snowbridge ports are reachable through NordVPN's firewall.
    # These settings persist but are re-applied here to be explicit.
    nordvpn allowlist add port 445 protocol TCP >/dev/null 2>&1 || true
    nordvpn allowlist add port 443 protocol TCP >/dev/null 2>&1 || true
    nordvpn allowlist add port 51820 protocol UDP >/dev/null 2>&1 || true
    nordvpn connect
else
    echo "    warning: nordvpn CLI not found, skipping"
fi

# ── 4. Samba ─────────────────────────────────────────────────────────────────
echo "==> Starting Samba…"
systemctl start smb nmb
echo "    smb/nmb: $(systemctl is-active smb) / $(systemctl is-active nmb)"

# ── 5. File Browser + Caddy ──────────────────────────────────────────────────
echo "==> Starting File Browser + Caddy…"

compose_cmd=()
if command -v podman-compose &>/dev/null; then
    compose_cmd=(podman-compose)
elif command -v docker &>/dev/null && docker compose version &>/dev/null 2>&1; then
    compose_cmd=(docker compose)
elif command -v docker-compose &>/dev/null; then
    compose_cmd=(docker-compose)
else
    echo "error: no supported compose runtime found (podman-compose / docker compose)" >&2
    exit 1
fi

"${compose_cmd[@]}" \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" \
    up -d --force-recreate

echo "==> snowbridge is up."
