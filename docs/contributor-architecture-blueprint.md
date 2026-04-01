# Contributor Architecture Blueprint

This document maps the current `snowbridge` operating model: repo-managed
templates and setup scripts drive a host-local Samba share, optional VPN-backed
private access, and optional browser-based access through a separate HTTPS
front end.

## High-Level Layers

1. Client surfaces
   - SMB clients are the default surface: iPhone Files plus other trusted
     devices use authenticated SMB read/write access.
   - Browser clients are optional and only appear when the File Browser plus
     Caddy stack is enabled.
2. Access-profile layer
   - LAN SMB remains the simplest baseline for local access.
   - Remote private access can use either `wireguard-public-vpn` for
     host-only reachability or `wireguard-lan-vpn` when the wider LAN should be
     reachable through the tunnel.
   - Optional HTTPS access is split into `private-vpn`, `private-vpn-mtls`,
     `public`, and `public-private-ip` profiles so the exposure boundary is
     explicit.
   - `private-vpn-mtls` now keeps device-certificate validation at Caddy while
     File Browser can stay on proxy-auth mode behind the reverse proxy.
3. Repo control layer
   - `README.md`, `docs/host-setup.md`, `docs/access-patterns.md`, and this
     blueprint explain the supported deployment modes.
   - `config/share-layout/`, `config/samba/`, `config/access/`, and
     `config/web/` hold the checked-in templates for share layout, network
     access, and optional web access.
   - `scripts/setup_bind_share.py`, `scripts/setup_wireguard.sh`,
     `scripts/setup_caddy_filebrowser.sh`, and
     `scripts/setup_filebrowser_access.py` convert those templates into host
     runtime state.
   - `scripts/export_caddy_root_profile.py`,
     `scripts/export_caddy_mtls_profile.py`, and
     `scripts/debug_private_access.sh` support phone trust bootstrap and
     multi-service troubleshooting.
4. Host runtime layer
   - Samba and the dedicated SMB account serve the share root.
   - The share root is a staging tree under `/srv/snowbridge/share` whose
     visible folders are often bind-mounted from elsewhere on the host.
   - WireGuard, dnsmasq, and firewalld implement private routing plus split DNS
     for VPN-backed access.
   - Caddy and File Browser implement the optional browser surface, including
     proxy-auth or mTLS gating at the HTTPS edge.
   - Exported `.mobileconfig` profiles are staged into the share so iPhone
     clients can install the trust or mTLS material.
5. Host-only state layer
   - Canonical files remain outside git in the real host folders that are
     bind-mounted into the share root.
   - Local configs, Samba passdb state, Caddy PKI material, generated client
     identities, and debug reports stay on the host and out of commits.

## Key Entry Points

- `README.md`
- `docs/host-setup.md`
- `docs/access-patterns.md`
- `config/share-layout/folders.example.ini`
- `config/samba/smb.conf.example`
- `config/access/wireguard/wg0-server.example.conf`
- `config/access/wireguard/wg0-server.lan-vpn.example.conf`
- `config/web/caddy/Caddyfile.private-vpn.example`
- `config/web/caddy/Caddyfile.private-vpn-mtls.example`
- `config/web/caddy/Caddyfile.public.example`
- `config/web/caddy/Caddyfile.public-private-ip.example`
- `config/web/filebrowser/access.example.toml`
- `scripts/setup_bind_share.py`
- `scripts/setup_wireguard.sh`
- `scripts/setup_caddy_filebrowser.sh`
- `scripts/setup_filebrowser_access.py`
- `scripts/export_caddy_root_profile.py`
- `scripts/export_caddy_mtls_profile.py`
- `scripts/debug_private_access.sh`
- `docs/diagrams/repo-architecture.puml`
- `docs/diagrams/repo-architecture.drawio`

## Regeneration

```bash
cd ../archility
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m archility render ../snowbridge
```

## Contributor Notes

- Keep the distinction explicit between repo-managed templates and host-held
  state.
- Model SMB, VPN, and HTTPS as separate access profiles rather than collapsing
  them into one generic “remote access” path.
- When `private-vpn-mtls` changes, document both the Caddy client-certificate
  gate and the File Browser auth mode so the identity handoff stays clear.
- Treat generated CA bundles, client identities, Samba credentials, and debug
  reports as host-only artifacts.
- Update `README.md`, `docs/host-setup.md`, `docs/access-patterns.md`, and the
  diagram sources together when the access model changes.
