#!/usr/bin/env bash
# nordvpn_rotate.sh — rotate NordVPN server and re-apply WireGuard bypass rules
#
# NordVPN's disconnect phase flushes ip rules, removing the wg0 fwmark rule
# that keeps WireGuard responses on the real internet gateway instead of
# nordlynx.  This script reconnects NordVPN then immediately re-applies the
# socket fwmark and ip rule so the WireGuard tunnel to the phone stays up.
#
# Usage:
#   sudo bash scripts/nordvpn_rotate.sh [nordvpn connect args...]
#
# Examples:
#   sudo bash scripts/nordvpn_rotate.sh           # connect to best server
#   sudo bash scripts/nordvpn_rotate.sh --country US

set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "error: run as root (sudo)" >&2
    exit 1
fi

echo "==> Rotating NordVPN…"
nordvpn connect "$@"

if command -v wg &>/dev/null && ip link show wg0 &>/dev/null 2>&1; then
    WG_FWMARK=51820   # 0xca54
    wg set wg0 fwmark "${WG_FWMARK}"
    ip rule show | grep -q "0xca54" || \
        ip rule add fwmark "${WG_FWMARK}" lookup main priority 100
    echo "    wg0 NordVPN bypass: re-applied"
fi
