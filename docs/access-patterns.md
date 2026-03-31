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

## 3. WireGuard Public-VPN

Use this when the public internet should see only the WireGuard UDP listener,
while SMB and the optional private web UI remain reachable only after VPN auth.

Templates:

- [wg0-server.example.conf](/mnt/4tb-m2/git/util-repos/snowbridge/config/access/wireguard/wg0-server.example.conf)
- [iphone-peer.example.conf](/mnt/4tb-m2/git/util-repos/snowbridge/config/access/wireguard/iphone-peer.example.conf)
- [setup_wireguard.sh](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/setup_wireguard.sh)

Recommended first step:

- connect to the host's tunnel IP or private hostname over WireGuard, for
  example `smb://10.99.0.1` or `https://files.snowbridge.internal`

Suggested flow:

1. Run `./scripts/setup_wireguard.sh --init-local-configs --profile wireguard-public-vpn`.
2. Edit `config/access/wireguard/wg0-server.public-vpn.local.conf`.
3. Edit `config/access/wireguard/iphone-peer.public-vpn.local.conf`.
4. Run `sudo ./scripts/setup_wireguard.sh --profile wireguard-public-vpn --print-iphone-qr`.

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

## 4. WireGuard LAN-VPN

Use this when the iPhone or other client should route the wider home LAN
through the tunnel, not just the Snowbridge host itself.

Templates:

- [wg0-server.lan-vpn.example.conf](/mnt/4tb-m2/git/util-repos/snowbridge/config/access/wireguard/wg0-server.lan-vpn.example.conf)
- [iphone-peer.lan-vpn.example.conf](/mnt/4tb-m2/git/util-repos/snowbridge/config/access/wireguard/iphone-peer.lan-vpn.example.conf)
- [setup_wireguard.sh](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/setup_wireguard.sh)

Suggested flow:

1. Run `./scripts/setup_wireguard.sh --init-local-configs --profile wireguard-lan-vpn --lan-subnet 192.168.0.0/24`.
2. Edit `config/access/wireguard/wg0-server.lan-vpn.local.conf`.
3. Edit `config/access/wireguard/iphone-peer.lan-vpn.local.conf`.
4. Run `sudo ./scripts/setup_wireguard.sh --profile wireguard-lan-vpn --lan-subnet 192.168.0.0/24 --enable-ip-forward --print-iphone-qr`.

This profile expects the client-side `AllowedIPs` to include both the WireGuard
tunnel subnet and the real home LAN subnet. The setup script can fill the LAN
route automatically with `--lan-subnet`; otherwise it leaves the checked-in
`<lan-subnet-cidr>` placeholder in place until you replace it.

Because this profile routes traffic beyond the host itself, it normally also
needs IPv4 forwarding and matching firewall policy on the server.

## 5. Private HTTPS Web Access Behind a VPN

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

## 6. Private HTTPS Web Access Behind a VPN with mTLS

Use this when the service should stay inside the same private VPN boundary as
SMB, but the browser path should also require a per-device client certificate
before File Browser is reachable.

Templates:

- [docker-compose.example.yml](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/docker-compose.example.yml)
- [filebrowser.env.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/filebrowser.env.example)
- [access.example.toml](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/access.example.toml)
- [Caddyfile.private-vpn-mtls.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/caddy/Caddyfile.private-vpn-mtls.example)
- [setup_caddy_filebrowser.sh](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/setup_caddy_filebrowser.sh)
- [setup_filebrowser_access.py](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/setup_filebrowser_access.py)
- [export_caddy_mtls_profile.py](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/export_caddy_mtls_profile.py)

Suggested flow:

1. Run `./scripts/setup_caddy_filebrowser.sh --init-local-configs --mode private-vpn-mtls`.
2. Edit `config/web/filebrowser/filebrowser.env.local`.
3. Edit `config/web/caddy/Caddyfile.private-vpn-mtls.local`.
4. Run `./scripts/setup_filebrowser_access.py --init-local-configs`.
5. Edit `config/web/filebrowser/access.local.toml`.
6. Run `sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn-mtls`.
7. Run `sudo ./scripts/setup_filebrowser_access.py`.
8. Run `sudo ./scripts/export_caddy_mtls_profile.py --device-name iphone`.
9. Install the generated `.mobileconfig` from the SMB share on the iPhone and enter the printed identity password during import.
10. Enable trust for the bundled Snowbridge Caddy root CA in `Certificate Trust Settings`.

This mode keeps the same private-VPN-only bind and hostname pattern as
`private-vpn`, but adds Caddy `client_auth` backed by a host-local client CA at
`CADDY_DATA_DIR/mtls/client-ca.crt`.
The setup script creates that CA automatically if it is missing, and the mTLS
profile exporter then issues a per-device identity from it.
The generated `.mobileconfig` includes both the local Caddy root CA and the
client identity, while the fallback staged `.p12` file gives you a manual
import path if the profile workflow fails on the device.

## 7. Public HTTPS Web Access

Use this only if you explicitly want browser access from the public internet and
accept the extra operational risk.

Templates:

- [docker-compose.example.yml](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/docker-compose.example.yml)
- [filebrowser.env.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/filebrowser.env.example)
- [access.example.toml](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/access.example.toml)
- [Caddyfile.public.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/caddy/Caddyfile.public.example)
- [Caddyfile.public-private-ip.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/caddy/Caddyfile.public-private-ip.example)
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

## 8. Public HTTPS via Router/NAT to a Private Host IP

Use this when the service is meant to be reachable from the public internet but
the home router forwards public `80/443` to a specific private RFC1918 address
on the host. This keeps the host-side bind narrower than `0.0.0.0` while still
publishing the HTTPS service externally.

Templates:

- [docker-compose.example.yml](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/docker-compose.example.yml)
- [filebrowser.env.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/filebrowser.env.example)
- [access.example.toml](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/filebrowser/access.example.toml)
- [Caddyfile.public-private-ip.example](/mnt/4tb-m2/git/util-repos/snowbridge/config/web/caddy/Caddyfile.public-private-ip.example)
- [setup_caddy_filebrowser.sh](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/setup_caddy_filebrowser.sh)
- [setup_filebrowser_access.py](/mnt/4tb-m2/git/util-repos/snowbridge/scripts/setup_filebrowser_access.py)

Minimum hardening expectations:

- expose only HTTPS, never TCP 445
- use a real domain with working public DNS
- bind Caddy only on the host's private RFC1918 address
- forward router public `80/443` only to that same private host IP
- require authentication at the web layer
- keep the web stack patched independently of Samba

Suggested flow:

1. Run `./scripts/setup_caddy_filebrowser.sh --init-local-configs --mode public-private-ip`.
2. Verify the detected bind IP in `config/web/filebrowser/filebrowser.env.local`.
3. Edit `config/web/caddy/Caddyfile.public-private-ip.local`.
4. Run `./scripts/setup_filebrowser_access.py --init-local-configs`.
5. Edit `config/web/filebrowser/access.local.toml`.
6. Run `sudo ./scripts/setup_caddy_filebrowser.sh --mode public-private-ip`.
7. Run `sudo ./scripts/setup_filebrowser_access.py`.
8. Forward router public `80/443` to the same private IP configured in
   `CADDY_HTTP_BIND` and `CADDY_HTTPS_BIND`.
9. If you later change mounts, labels, ports, or image definitions, rerun with
   `sudo ./scripts/setup_caddy_filebrowser.sh --mode public-private-ip --recreate`.

The setup script will try to auto-detect a private bind IP during
`--init-local-configs`. If it cannot, or if the detected IP is wrong for the
intended LAN interface, edit the env file manually before the first `sudo` run.
