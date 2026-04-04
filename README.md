# snowbridge

Personal fileshare utility repo for serving a read/write home-desktop share to
trusted devices.

This repo lives under:

- `./util-repos/snowbridge`

## Purpose

- Version-control the host configuration and operating notes for a personal
  fileshare.
- Keep authenticated iPhone read/write access as a first-class requirement.
- Keep share data, credentials, and host-local state outside the git repo.
- Prefer access over the home LAN or a private VPN overlay.

## Why SMB

`snowbridge` is Samba-first because the iOS Files app can connect to SMB
servers natively and supports authenticated read/write access.

That makes SMB the lowest-friction path for iPhone access without forcing a
custom client app or a separate sync workflow.

## Repository Layout

- `config/samba/smb.conf.example`: baseline Samba share configuration
- `config/share-layout/folders.example.ini`: bind-mounted folder layout example
- `config/network/`: stable-address examples for the host network
- `config/access/wireguard/`: WireGuard config examples for the `wireguard-public-vpn` and `wireguard-lan-vpn` profiles; use `./util-repos/short-circuit/scripts/setup_wireguard.sh` to install them
- `config/clockwork/`: scheduler templates rendered through the shared `clockwork` repo
- `config/access/wireguard/endpoint-monitor.example.toml`: example local-only monitor config for direct-IP WireGuard endpoint drift detection and notification
- `config/access/tailscale/`: Tailscale subnet router example
- `config/web/`: optional Caddy and File Browser templates for web access, including private-VPN HTTPS, private-VPN HTTPS with mTLS client certificates, and public HTTPS modes that can bind on either all interfaces or a specific private host IP behind router/NAT forwarding
- `scripts/setup_bind_share.py`: creates mountpoints, ACLs, and bind mounts
- `scripts/remount_luks_share.sh`: refreshes fstab bind mounts whose sources are on LUKS ext4 drives, for running after LUKS volumes are unlocked
- `scripts/start_snowbridge.sh`: single post-LUKS startup script — refreshes bind mounts, starts Samba, and brings up the File Browser + Caddy stack; append to your LUKS bootstrap
- `scripts/check_wireguard_endpoint.py`: detects public-WAN endpoint drift for direct-IP WireGuard client profiles, rewrites the local client configs, regenerates QR PNGs, and notifies through `shock-relay`
- `scripts/setup_wireguard_endpoint_monitor.sh`: initializes the local endpoint-monitor config and installs a periodic systemd timer for `check_wireguard_endpoint.py`
- `scripts/setup_caddy_filebrowser.sh`: prepares and launches the optional web stack in `private-vpn`, `private-vpn-mtls`, `public`, or `public-private-ip` mode, installing a supported container runtime and Compose frontend when needed, with optional local-browser bootstrap for hostname mapping and Caddy CA trust
- `scripts/setup_filebrowser_access.py`: applies File Browser root, users, auth mode, and runtime UID/GID sync from a local TOML config
- `scripts/export_caddy_root_profile.py`: generates an iPhone-installable `.mobileconfig` for Caddy's local CA and stages it into the SMB share
- `scripts/export_caddy_mtls_profile.py`: issues a per-device mTLS client identity, packages it with the private Caddy root CA into an iPhone-installable `.mobileconfig`, and stages the results into the SMB share
- `scripts/debug_private_access.sh`: collects a single report covering WireGuard, dnsmasq, firewalld, Samba, Caddy, and File Browser state for private-access debugging
- `docs/host-setup.md`: host-side setup and client connection notes
- `docs/iphone-shortcut.md`: iPhone shortcut and import/export guidance
- `docs/access-patterns.md`: optional access templates and risk tradeoffs
- `docs/contributor-architecture-blueprint.md`: contributor-facing architecture
- `docs/diagrams/repo-architecture.puml`: PlantUML architecture source
- `docs/diagrams/repo-architecture.drawio`: draw.io architecture source

## Quick Start

1. Install Samba on the home desktop.
2. Create a dedicated local account such as `snowbridge`.
3. Create the share root outside the repo, for example `/srv/snowbridge/share`.
4. Adapt `config/share-layout/folders.local.ini` so the share root exposes
   bind-mounted folders from elsewhere on the host.
5. Run `scripts/setup_bind_share.py` to create mountpoints, ACLs, and bind
   mounts from that layout.
6. Adapt `config/samba/smb.conf.example` into the host Samba configuration.
7. Create the Samba password for the dedicated account and validate the config
   with `testparm`.
8. Start the Samba service and allow LAN-only SMB access through the firewall.
9. On iPhone, open Files, choose `Browse`, then `...`, then `Connect to
   Server`, and connect to `smb://<desktop-hostname-or-ip>`.
10. For remote access, connect through a VPN overlay first. Do not expose SMB
   directly to the public internet.

### After LUKS Unlock (each session)

If any share folders are sourced from LUKS-encrypted drives, the fstab bind
mounts run at boot before those drives are unlocked and will be stale. Append
`start_snowbridge.sh` to your LUKS bootstrap script, or run it manually after
unlocking:

```bash
sudo bash scripts/start_snowbridge.sh
```

This refreshes the bind mounts, starts WireGuard, NordVPN (with the socket
fwmark and ip rule needed to keep WireGuard responses off nordlynx), Samba,
and the File Browser + Caddy container stack in one step.

### Rotating NordVPN while snowbridge is running

NordVPN's disconnect phase flushes ip rules, which removes the WireGuard
bypass rule. Use `nordility` to rotate so the rule is re-applied automatically:

```bash
sudo nordility change --restore-wireguard --wireguard-fwmark 51820
```

Plain `nordvpn connect` will work for NordVPN itself but will break the
WireGuard tunnel to the phone until `start_snowbridge.sh` is re-run.

See `docs/host-setup.md` for the detailed workflow, including hostname/IP
discovery, stable-address guidance, the split between `wireguard-public-vpn`,
`wireguard-lan-vpn`, `private-vpn-mtls`, and public HTTPS access, and optional
web access notes.
See `docs/access-patterns.md` for the concrete template files backing the
optional static-IP, VPN, and HTTPS access patterns.

## WireGuard Setup

WireGuard installation and configuration tooling is provided by
`./util-repos/short-circuit`. The config templates in `config/access/wireguard/`
contain the snowbridge-specific profile examples for `wireguard-public-vpn` and
`wireguard-lan-vpn`. Use `short-circuit` to initialize and install them:

```bash
# from short-circuit repo root
./scripts/setup_wireguard.sh \
  --init-local-configs \
  --profile wireguard-public-vpn \
  --server-config /path/to/snowbridge/config/access/wireguard/wg0-server.public-vpn.local.conf \
  --client-config /path/to/snowbridge/config/access/wireguard/iphone-peer.public-vpn.local.conf

sudo ./scripts/setup_wireguard.sh \
  --profile wireguard-public-vpn \
  --server-config /path/to/snowbridge/config/access/wireguard/wg0-server.public-vpn.local.conf \
  --client-config /path/to/snowbridge/config/access/wireguard/iphone-peer.public-vpn.local.conf \
  --dns-hostname files.snowbridge.internal \
  --print-client-qr
```

See `./util-repos/short-circuit/docs/setup-guide.md` for the full walkthrough.

## WireGuard Endpoint Drift Monitoring

If your iPhone WireGuard profiles use a raw public IP in `Endpoint = ...`
instead of a stable DNS name, add the local endpoint monitor so WAN-IP changes
regenerate the QR artifacts and notify you automatically.

Suggested flow:

```bash
./scripts/setup_wireguard_endpoint_monitor.sh --init-local-configs
# edit config/access/wireguard/endpoint-monitor.local.toml
python3 ./scripts/check_wireguard_endpoint.py --dry-run
sudo ./scripts/setup_wireguard_endpoint_monitor.sh --install-systemd
```

The local monitor config keeps recipient addresses and `shock-relay` config
paths outside git. The installed timer runs the check every 15 minutes by
default, rewrites any direct-IP client profiles whose `Endpoint` no longer
matches the current WAN IP, regenerates all configured QR PNGs, and sends the
latest endpoint through both email and Signal when enabled.
The installer now renders the systemd service and timer through the sibling
`clockwork` repo instead of writing the unit text inline here.
If you later move the client profiles to a stable DNS name or DDNS endpoint,
this monitor is no longer necessary.

## Contributing

See `CONTRIBUTING.md`.

## License

Private-use only. See `LICENSE`.
