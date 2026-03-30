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

The setup script installs missing `wireguard-tools` automatically, and it adds
`qrencode` when you request terminal or PNG QR output. By default it also
installs and configures `dnsmasq` so the private web hostname from the private
Caddy config resolves over the WireGuard tunnel, and on firewalld-based hosts
it also assigns the WireGuard interface to the trusted zone so private DNS and
HTTPS traffic can actually reach the host.
The same script also generates matching server and iPhone key pairs when those
paired placeholders are still present in the local configs. If the iPhone peer
`Endpoint` is still on the checked-in sample value, the script will replace it
with the host's current public IP and warn that you should still move to a
stable DNS name or other stable public endpoint.

## 4. Private HTTPS Web Access Behind a VPN

Use this when you want browser access in addition to SMB, but still want to
keep the service inside a private VPN boundary.

Templates:

- [docker-compose.example.yml](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/docker-compose.example.yml)
- [filebrowser.env.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/filebrowser.env.example)
- [access.example.toml](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/access.example.toml)
- [Caddyfile.private-vpn.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/caddy/Caddyfile.private-vpn.example)
- [setup_caddy_filebrowser.sh](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/setup_caddy_filebrowser.sh)
- [setup_filebrowser_access.py](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/setup_filebrowser_access.py)
- [export_caddy_root_profile.py](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/export_caddy_root_profile.py)

Suggested pattern:

1. Run File Browser on `127.0.0.1:8080`.
2. Reverse-proxy it through Caddy with internal TLS.
3. Expose the HTTPS endpoint only on the VPN boundary, using a private hostname
   that WireGuard clients resolve through the VPN DNS helper.
4. Keep SMB itself limited to the LAN or VPN.

Suggested flow:

1. Run `./scripts/setup_caddy_filebrowser.sh --init-local-configs --mode private-vpn`.
2. Edit `config/web/filebrowser/filebrowser.env.local`.
3. Edit `config/web/caddy/Caddyfile.private-vpn.local`.
4. Run `./scripts/setup_filebrowser_access.py --init-local-configs`.
5. Edit `config/web/filebrowser/access.local.toml`.
6. Run `sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn`.
7. Run `sudo ./scripts/setup_filebrowser_access.py`.
8. If you later change mounts, labels, ports, or image definitions, rerun with
   `sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn --recreate`.
9. If you later change File Browser users, scopes, root, or runtime UID/GID,
   rerun `sudo ./scripts/setup_filebrowser_access.py`.
10. If you want to browse the site from the desktop host itself, run
   `sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn --bootstrap-local-browser`.
11. If the iPhone will not install the raw root certificate cleanly, run
    `sudo ./scripts/export_caddy_root_profile.py` and install the generated
    `snowbridge-caddy-local-root.mobileconfig` from the SMB share instead.

The setup script installs a supported local container runtime and Compose
frontend automatically when they are missing. On Fedora-class systems it
prefers `podman` plus `podman-compose`.

For iPhone access behind WireGuard, use the private hostname served by Caddy,
for example `https://files.snowbridge.internal`. The WireGuard setup script now
installs a small split-DNS resolver so that hostname resolves to the tunnel IP
for VPN clients.
The profile-export script stages an Apple configuration profile in
`/srv/snowbridge/share/tmp/` by default so the iPhone can install and trust the
local Caddy CA through Files plus `Certificate Trust Settings`.
If the private browser path still fails after reconnecting WireGuard and
recreating the web stack, run `sudo ./scripts/debug_private_access.sh` to write
a timestamped troubleshooting report under `reports/`.

On SELinux-enforcing Fedora-class hosts, the compose template also uses SELinux
relabeling for the mounted host paths.
The File Browser container listens on port `8080` internally so it can run as a
non-root user while Caddy proxies to it over the compose network.
For host-local browsing with `tls internal`, the setup script can also install
the generated Caddy root CA into host trust and add a local hosts entry for the
configured site hostname.
The File Browser access script can also sync the runtime UID/GID in
`filebrowser.env.local` to the configured host account before recreating the
stack, which avoids the "web user can log in but cannot see files" failure mode
when the share root is only traversable by the dedicated SMB account.
If a pasted password line in `access.local.toml` is not valid TOML yet, the
access script will normalize fixable cases automatically and fail only when the
line is too ambiguous to repair safely.

## 5. Public HTTPS Web Access

Use this only if you explicitly want browser access from the public internet and
accept the extra operational risk.

Templates:

- [docker-compose.example.yml](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/docker-compose.example.yml)
- [filebrowser.env.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/filebrowser.env.example)
- [access.example.toml](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/access.example.toml)
- [Caddyfile.public.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/caddy/Caddyfile.public.example)
- [setup_caddy_filebrowser.sh](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/setup_caddy_filebrowser.sh)
- [setup_filebrowser_access.py](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/setup_filebrowser_access.py)

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
4. Run `./scripts/setup_filebrowser_access.py --init-local-configs`.
5. Edit `config/web/filebrowser/access.local.toml`.
6. Run `sudo ./scripts/setup_caddy_filebrowser.sh --mode public`.
7. Run `sudo ./scripts/setup_filebrowser_access.py`.
8. If you later change mounts, labels, ports, or image definitions, rerun with
   `sudo ./scripts/setup_caddy_filebrowser.sh --mode public --recreate`.
9. If you later change File Browser users, scopes, root, or runtime UID/GID,
   rerun `sudo ./scripts/setup_filebrowser_access.py`.

The same installer behavior applies here: missing container runtime or Compose
dependencies are installed automatically when a supported package manager is
available.

The same SELinux note applies here as well: the compose template uses `:Z`
mount options for Podman-friendly bind relabeling on Fedora-class hosts.
The same internal-port behavior applies here too: Caddy proxies to File Browser
on `filebrowser:8080`.
