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
- `docs/host-setup.md`: host-side setup and client connection notes
- `docs/contributor-architecture-blueprint.md`: contributor-facing architecture
- `docs/diagrams/repo-architecture.puml`: PlantUML architecture source
- `docs/diagrams/repo-architecture.drawio`: draw.io architecture source

## Quick Start

1. Install Samba on the home desktop.
2. Create a dedicated local account such as `snowbridge`.
3. Create the share root outside the repo, for example `/srv/snowbridge/share`.
4. Adapt `config/samba/smb.conf.example` into the host Samba configuration.
5. Create the Samba password for the dedicated account and validate the config
   with `testparm`.
6. Start the Samba service and allow LAN-only SMB access through the firewall.
7. On iPhone, open Files, choose `Browse`, then `...`, then `Connect to
   Server`, and connect to `smb://<desktop-hostname-or-ip>`.
8. For remote access, connect through a VPN overlay first. Do not expose SMB
   directly to the public internet.

See `docs/host-setup.md` for the detailed workflow.

## Contributing

See `CONTRIBUTING.md`.

## License

Private-use only. See `LICENSE`.
