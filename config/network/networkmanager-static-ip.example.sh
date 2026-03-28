#!/usr/bin/env bash
set -euo pipefail

# snowbridge static IPv4 example for NetworkManager-managed hosts
#
# Prefer a DHCP reservation in the home router when possible. Use this template
# only when you want the host itself to hold a static private address.

CONNECTION_NAME="<connection-name>"
IPV4_CIDR="192.168.1.50/24"
IPV4_GATEWAY="192.168.1.1"
IPV4_DNS="192.168.1.1 1.1.1.1"

nmcli connection show
sudo nmcli connection modify "${CONNECTION_NAME}" \
  ipv4.addresses "${IPV4_CIDR}" \
  ipv4.gateway "${IPV4_GATEWAY}" \
  ipv4.dns "${IPV4_DNS}" \
  ipv4.method manual \
  ipv6.method auto
sudo nmcli connection up "${CONNECTION_NAME}"
