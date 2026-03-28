# Contributor Architecture Blueprint

This document maps the operating model implemented by `snowbridge`.

The repository itself is not the share payload. It is the control surface for a
personal Samba deployment: configuration templates, deployment notes, and
architecture handoff documentation for a home-desktop fileshare.

## High-Level Layers

1. Client layer
   - iPhone uses the iOS Files app to connect over SMB with authenticated
     read/write access.
   - Other trusted devices such as laptops or tablets connect over the same SMB
     endpoint.
2. Network-access layer
   - Access is intended for the home LAN first.
   - Remote access, when needed, should traverse a private VPN overlay instead
     of exposing SMB to the public internet.
   - Stable private addressing should come from local hostname resolution or a
     reserved private IP rather than a frequently changing DHCP lease.
3. Share-service layer
   - Samba is the baseline implementation because it is natively compatible with
     iPhone and other common client platforms.
   - `config/samba/smb.conf.example` is the primary checked-in service template.
   - `scripts/setup_bind_share.py` materializes the share root from a
     repo-managed bind layout before Samba serves it.
   - `scripts/setup_wireguard.sh` and `scripts/setup_caddy_filebrowser.sh`
     operationalize the optional remote-access and web-access templates.
4. Host-storage layer
   - The share root lives outside the repo, for example
     `/srv/snowbridge/share`.
   - External host folders are bind-mounted into that share root rather than
     moving the actual files under `/srv`.
   - Samba account state, logs, and runtime metadata stay on the host and are
     not committed.
   - If browser-based access is added later, it should arrive as a separate
     HTTPS-facing service layer rather than as public SMB exposure.
5. Repo-governance layer
   - `README.md` explains the purpose and quick-start path.
   - `docs/host-setup.md` captures the deployment workflow.
   - `docs/access-patterns.md` captures the optional static-IP, VPN, and HTTPS
     access templates.
   - `config/share-layout/` holds the bind-mounted folder layout templates.
   - `config/network/`, `config/access/`, and `config/web/` hold optional
     network and web-access templates.
   - `AGENTS.md` and `LESSONSLEARNED.md` keep repo-specific operating guidance
     durable.
   - `docs/diagrams/` is the architecture handoff surface.

## Key Entry Points

- `README.md`
- `config/samba/smb.conf.example`
- `config/share-layout/folders.example.ini`
- `config/network/networkmanager-static-ip.example.sh`
- `config/access/tailscale/tailscale-subnet-router.example.sh`
- `config/access/wireguard/wg0-server.example.conf`
- `config/web/caddy/Caddyfile.private-vpn.example`
- `scripts/setup_bind_share.py`
- `scripts/setup_wireguard.sh`
- `scripts/setup_caddy_filebrowser.sh`
- `docs/host-setup.md`
- `docs/access-patterns.md`
- `docs/diagrams/repo-architecture.puml`
- `docs/diagrams/repo-architecture.drawio`

## Regeneration

```bash
cd ../archility
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m archility render ../snowbridge
```

## Contributor Notes

- Keep the distinction explicit between repo-managed configuration and host-held
  data.
- Prefer bind mounts for exposing folders outside the share root. Do not fall
  back to Samba `wide links` unless the user explicitly accepts the security
  tradeoff.
- Keep direct SMB limited to the LAN or a private VPN. If a web surface is
  added later, document it as a distinct HTTPS front end.
- Update the Samba template, host setup guide, and architecture docs together
  when the access model changes.
- Preserve iPhone compatibility unless the user explicitly chooses a different
  design.
