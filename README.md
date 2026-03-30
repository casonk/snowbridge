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
- `config/access/`: VPN templates for Tailscale and WireGuard
- `config/web/`: optional Caddy and File Browser templates for web access, including private-VPN HTTPS behind a private hostname
- `scripts/setup_bind_share.py`: creates mountpoints, ACLs, and bind mounts
- `scripts/setup_wireguard.sh`: installs a local WireGuard config, required tools, auto-generates missing peer keys, configures split DNS and firewalld for private WireGuard clients, auto-fills the iPhone peer endpoint from the current public IP when needed, validates the remaining peer values, and can render an optional iPhone QR
- `scripts/setup_caddy_filebrowser.sh`: prepares and launches the optional web stack, installing a supported container runtime and Compose frontend when needed, with optional local-browser bootstrap for hostname mapping and Caddy CA trust
- `scripts/setup_filebrowser_access.py`: applies File Browser root, users, and runtime UID/GID sync from a local TOML config
- `scripts/export_caddy_root_profile.py`: generates an iPhone-installable `.mobileconfig` for Caddy's local CA and stages it into the SMB share
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

See `docs/host-setup.md` for the detailed workflow, including hostname/IP
discovery, stable-address guidance, VPN access patterns, and optional web access
notes. See `docs/access-patterns.md` for the concrete template files backing the
optional static-IP, VPN, and HTTPS access patterns.

## Contributing

See `CONTRIBUTING.md`.

## License

Private-use only. See `LICENSE`.
