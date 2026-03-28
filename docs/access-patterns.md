# Access Patterns

This document collects concrete templates for the optional access patterns
mentioned in `docs/host-setup.md`.

The safety baseline remains:

- LAN-only SMB: lowest complexity
- private-VPN SMB: recommended remote-access path
- separate HTTPS web front end: optional and riskier than VPN-only access
- direct public SMB exposure: not allowed

## 1. Stable Private IP

Preferred default:

- reserve a DHCP lease in the home router for the `snowbridge` host

If you want the host itself to hold a static private IPv4 address, use:

- [networkmanager-static-ip.example.sh](/mnt/4tb-m2/git/util-repos/snowbridge/config/network/networkmanager-static-ip.example.sh)

Notes:

- The template assumes NetworkManager on a Linux desktop.
- Replace the placeholder connection name, IP, gateway, and DNS values before
  running it.
- Reconnect iPhone clients with the new `smb://<hostname-or-ip>` value after
  the address changes.

## 2. Tailscale

Use Tailscale when you want low-friction private remote access and do not want
to manage WireGuard peer distribution by hand.

Template:

- [tailscale-subnet-router.example.sh](/mnt/4tb-m2/git/util-repos/snowbridge/config/access/tailscale/tailscale-subnet-router.example.sh)

Patterns covered by the template:

- direct host access from iPhone to the `snowbridge` host
- optional subnet-router mode for reaching the full home LAN

Recommended first step:

- install Tailscale directly on the `snowbridge` host and connect from iPhone to
  the host's Tailscale name or address

## 3. WireGuard

Use WireGuard when you want a fully self-managed private VPN with explicit peer
configs.

Templates:

- [wg0-server.example.conf](/mnt/4tb-m2/git/util-repos/snowbridge/config/access/wireguard/wg0-server.example.conf)
- [iphone-peer.example.conf](/mnt/4tb-m2/git/util-repos/snowbridge/config/access/wireguard/iphone-peer.example.conf)
- [setup_wireguard.sh](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/setup_wireguard.sh)

Recommended first step:

- start with host-only access and connect from iPhone to the host's WireGuard
  tunnel IP, for example `smb://10.99.0.1`

Only widen the routing scope to the full home LAN if you explicitly need it.

Suggested flow:

1. Run `./scripts/setup_wireguard.sh --init-local-configs`.
2. Edit `config/access/wireguard/wg0-server.local.conf`.
3. Edit `config/access/wireguard/iphone-peer.local.conf`.
4. Run `sudo ./scripts/setup_wireguard.sh --enable-ip-forward --print-iphone-qr`.

## 4. Private HTTPS Web Access Behind a VPN

Use this when you want browser access in addition to SMB, but still want to
keep the service inside a private VPN boundary.

Templates:

- [docker-compose.example.yml](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/docker-compose.example.yml)
- [filebrowser.env.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/filebrowser.env.example)
- [Caddyfile.private-vpn.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/caddy/Caddyfile.private-vpn.example)
- [setup_caddy_filebrowser.sh](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/setup_caddy_filebrowser.sh)

Suggested pattern:

1. Run File Browser on `127.0.0.1:8080`.
2. Reverse-proxy it through Caddy with internal TLS.
3. Expose the HTTPS endpoint only on the VPN boundary.
4. Keep SMB itself limited to the LAN or VPN.

Suggested flow:

1. Run `./scripts/setup_caddy_filebrowser.sh --init-local-configs --mode private-vpn`.
2. Edit `config/web/filebrowser/filebrowser.env.local`.
3. Edit `config/web/caddy/Caddyfile.private-vpn.local`.
4. Run `sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn`.

## 5. Public HTTPS Web Access

Use this only if you explicitly want browser access from the public internet and
accept the extra operational risk.

Templates:

- [docker-compose.example.yml](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/docker-compose.example.yml)
- [filebrowser.env.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/filebrowser.env.example)
- [Caddyfile.public.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/caddy/Caddyfile.public.example)
- [setup_caddy_filebrowser.sh](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/setup_caddy_filebrowser.sh)

Minimum hardening expectations:

- expose only HTTPS, never TCP 445
- use a real domain with working DNS
- require authentication at the web layer
- keep the web stack patched independently of Samba
- log access and review the logs
- keep the served filesystem scope narrower than "everything on the host"

If in doubt, prefer the private VPN-only web pattern instead.

Suggested flow:

1. Run `./scripts/setup_caddy_filebrowser.sh --init-local-configs --mode public`.
2. Edit `config/web/filebrowser/filebrowser.env.local`.
3. Edit `config/web/caddy/Caddyfile.public.local`.
4. Run `sudo ./scripts/setup_caddy_filebrowser.sh --mode public`.
