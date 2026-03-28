#!/usr/bin/env bash
set -euo pipefail

# snowbridge Tailscale example
#
# This template covers two common patterns:
# 1. direct host access from iPhone to the snowbridge host
# 2. optional subnet-router mode for reaching the whole home LAN
#
# Replace the placeholder values before running any command.

TAILSCALE_HOSTNAME="snowbridge"
TAILSCALE_AUTH_KEY="<optional-auth-key>"
HOME_LAN_SUBNET="192.168.1.0/24"

# Install and enable tailscaled using the distro-appropriate package source
# before using the commands below.

sudo systemctl enable --now tailscaled

# Direct-host access only:
# sudo tailscale up \
#   --hostname="${TAILSCALE_HOSTNAME}" \
#   --auth-key="${TAILSCALE_AUTH_KEY}"

# Optional subnet-router mode for reaching the home LAN through this host:
# echo "net.ipv4.ip_forward = 1" | sudo tee /etc/sysctl.d/99-tailscale.conf
# echo "net.ipv6.conf.all.forwarding = 1" | sudo tee -a /etc/sysctl.d/99-tailscale.conf
# sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
# sudo tailscale up \
#   --hostname="${TAILSCALE_HOSTNAME}" \
#   --auth-key="${TAILSCALE_AUTH_KEY}"
# sudo tailscale set --advertise-routes="${HOME_LAN_SUBNET}"

# After subnet-router setup, approve the advertised routes in the Tailscale
# admin console before relying on them from iPhone.
