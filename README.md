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
- Onboard cloud accounts through an encrypted, local-only rclone config before
  any selected folder is exposed through Snowbridge.
- Consent reference: [`../../doc-repos/my-consent/remote-access-and-private-files.md`](../../doc-repos/my-consent/remote-access-and-private-files.md) documents the explicit consent covering personal file-sharing, device-profile, certificate, and remote-access processing handled by this repo.

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
- `config/cloud/accounts.example.toml`: synthetic, inactive account registry;
  live aliases and paths belong in the ignored owner-only local registry
- `config/macos/air-smb.example.toml`: native macOS SMB render-only template;
  the ignored local copy carries the exact Air share path, account, and
  WireGuard `/32` addresses
- `config/macos/air-filebrowser.example.toml`: rootless Air Podman backend
  template with an exact loopback bind and digest-pinned official image
- `config/access/wireguard/endpoint-monitor.example.toml`: example local-only monitor config for direct-IP WireGuard endpoint drift detection and notification
- `config/access/tailscale/`: Tailscale subnet router example
- `config/web/`: optional Caddy and File Browser templates for web access, including private-VPN HTTPS, private-VPN HTTPS with mTLS client certificates, and public HTTPS modes that can bind on either all interfaces or a specific private host IP behind router/NAT forwarding
- `scripts/setup_bind_share.py`: creates mountpoints, ACLs, and bind mounts
- `scripts/remount_luks_share.sh`: refreshes fstab bind mounts whose sources are on LUKS ext4 drives, for running after LUKS volumes are unlocked
- `scripts/start_snowbridge.sh`: single post-LUKS startup script — refreshes bind mounts, installs the bind-mount watchdog, starts Samba, and brings up the File Browser + Caddy stack; append to your LUKS bootstrap
- `scripts/repair_share_access.sh`: one-command Snowbridge repair wrapper; runs the standard startup flow and writes a debug report under `reports/` if recovery fails
- `scripts/check_share_bind_mounts.sh`: verifies and optionally repairs managed share bind mounts after source volumes are unlocked
- `scripts/setup_share_bind_mount_watch.sh`: installs a systemd timer that keeps managed share bind mounts current
- `scripts/check_wireguard_endpoint.py`: detects public-WAN endpoint drift for direct-IP WireGuard client profiles, rewrites the local client configs, regenerates QR PNGs, and notifies through `shock-relay`
- `scripts/setup_wireguard_endpoint_monitor.sh`: initializes the local endpoint-monitor config and installs a periodic systemd timer for `check_wireguard_endpoint.py`
- `scripts/setup_caddy_filebrowser.sh`: prepares and launches the optional web stack in `private-vpn`, `private-vpn-mtls`, `public`, or `public-private-ip` mode, installing a supported container runtime and Compose frontend when needed, with optional local-browser bootstrap for hostname mapping and Caddy CA trust
- `scripts/check_filebrowser_backend.sh`: probes the local File Browser backend and can restart the compose service when the HTTPS edge is up but File Browser is down
- `scripts/setup_filebrowser_backend_watch.sh`: installs the clockwork-rendered systemd timer that runs the File Browser backend watchdog
- `scripts/setup_filebrowser_access.py`: applies File Browser root, users, auth mode, and runtime UID/GID sync from a local TOML config
- `scripts/setup_filebrowser_fork_workspace.sh`: installs the local File Browser fork prerequisites and runs the upstream-style frontend/backend checks against `vendor/filebrowser-upstream`
- `scripts/build_filebrowser_fork_image.sh`: builds the patched File Browser binary from `vendor/filebrowser-upstream`, stages a minimal container context, and tags a local custom image for `FILEBROWSER_IMAGE`
- `scripts/deploy_filebrowser_fork_image.sh`: builds the local fork image, writes its tag into `config/web/filebrowser/filebrowser.env.local`, and recreates the File Browser + Caddy stack
- `scripts/export_caddy_root_profile.py`: generates an iPhone-installable `.mobileconfig` for Caddy's local CA and stages it into the SMB share
- `scripts/export_caddy_mtls_profile.py`: issues a per-device mTLS client identity, packages it with the private Caddy root CA into an iPhone-installable `.mobileconfig`, and stages the results into the SMB share
- `scripts/debug_private_access.sh`: collects a single report covering WireGuard, dnsmasq, firewalld, Samba, Caddy, and File Browser state for private-access debugging
- `scripts/cloud_accounts.py`: creates and validates the ignored cloud account
  registry and checks encrypted rclone aliases plus backend types without
  contacting configured storage backends
- `scripts/macos_smb_plan.py`: validates the native Air SMB boundary and
  renders owner-only PF/share-point review artifacts without changing macOS
- `scripts/macos_filebrowser_podman.py`: renders, bootstraps, starts, and reports
  the rootless loopback-only Air File Browser backend without touching SMB/PF
- `docs/cloud-storage.md`: inventory-only cloud onboarding and macOS Keychain
  workflow
- `docs/host-setup.md`: host-side setup and client connection notes
- `docs/iphone-shortcut.md`: iPhone shortcut and import/export guidance
- `docs/access-patterns.md`: optional access templates and risk tradeoffs
- `docs/filebrowser-directory-size-plan.md`: minimal custom-fork and upstream-PR plan for adding real File Browser folder sizes
- `docs/macos-air-filebrowser.md`: Air rootless Podman, proxy-auth, LaunchAgent,
  and activation contract
- `docs/contributor-architecture-blueprint.md`: contributor-facing architecture
- `docs/diagrams/repo-architecture.puml`: PlantUML architecture source
- `docs/diagrams/repo-architecture.drawio`: draw.io architecture source

## Native macOS Air Plan (Render Only)

The temporary Air host has a separate, fail-closed native macOS path. It does
not install Samba and does not reuse the Linux startup scripts. It plans an
Apple `smbd` share that is authenticated, guest-disabled, SMB3-encrypted, and
reachable only through an exact WireGuard `utun` interface and explicitly
allowed client `/32` addresses.

```bash
python3 scripts/macos_smb_plan.py init
# Edit config/macos/air-smb.local.toml and create its share directory as 0700.
python3 scripts/macos_smb_plan.py validate
python3 scripts/macos_smb_plan.py audit
python3 scripts/macos_smb_plan.py render
```

The local TOML is ignored and must be mode `0600`. Rendered files live under
ignored `artifacts/macos-air-smb/` with owner-only permissions. The audit fails
if macOS share inventory cannot be read, any enabled share permits guests, an
unrelated share is enabled, the configured `utun` `/32` is absent, or TCP 445
is exposed outside the WireGuard boundary. Native `smbd` may wildcard-listen
only after the exact PF anchor is independently verified.

macOS allocates `utunN` names dynamically. If WireGuard or another VPN toggle
changes the interface number, the active proof is invalid: rediscover the
interface carrying the configured host `/32`, update the ignored config, and
rerun audit/render. The `/32` is the identity source of truth; the renderer
never broadens PF to a wildcard `utun` match.

There is deliberately no live apply command. The renderer records the required
PF-first activation and reverse-order rollback, but it never loads PF, edits a
share point, toggles File Sharing, changes a macOS account, or handles a
password. A future live path must require explicit operator confirmation and
root, and must atomically restore both PF and share-point state on failure.

## Air File Browser Backend

The web backend has a separate rootless macOS implementation. It binds only
`127.0.0.1:8080`; Wiring Harness owns the mTLS-protected WireGuard listener on
port `8444`. Review first, then bootstrap without `sudo`:

```bash
python3 scripts/macos_filebrowser_podman.py validate
python3 scripts/macos_filebrowser_podman.py render
python3 scripts/macos_filebrowser_podman.py bootstrap
python3 scripts/macos_filebrowser_podman.py status
```

The manager pins the Podman machine/connection and official image digest,
runs as the current UID/GID, drops all capabilities, enables
`no-new-privileges`, makes the container root read-only, and installs a user
LaunchAgent for login startup. It never modifies the Public Folder, SMB, PF,
WireGuard, or a password. The LaunchAgent remains alive as an event-driven
health supervisor, stops only the labeled container on termination or health
loss, and lets launchd restart the validated path after a failure. See
`docs/macos-air-filebrowser.md` for the required Wiring Harness proxy-header
overwrite and complete activation boundary.

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

This refreshes the bind mounts, installs/enables the bind-mount watchdog,
starts WireGuard, NordVPN (with the socket fwmark and ip rule needed to keep
WireGuard responses off nordlynx), Samba, and the File Browser + Caddy
container stack in one step.
It also verifies that File Browser is serving the local web UI on
`127.0.0.1:8080`; if that check fails, the script exits with an error instead
of leaving Caddy reachable while the web backend is down.

If the share is still inaccessible and you want the same repair flow plus an
automatic debug capture on failure, run:

```bash
sudo bash scripts/repair_share_access.sh
```

If recovery fails, the script writes a timestamped report under `reports/` so
the next debugging pass has the full system evidence instead of only a partial
terminal transcript.

To keep the optional browser path self-healing after boot, container restarts,
or Podman state changes, install the File Browser backend watchdog:

```bash
sudo ./scripts/setup_filebrowser_backend_watch.sh --install-systemd
```

The timer checks every 5 minutes by default and runs `compose up -d` for the
File Browser service when the local backend probe fails. This complements the
post-LUKS startup script; it does not replace the bind-mount refresh after the
encrypted drives are unlocked.

If you are not using `start_snowbridge.sh`, install the bind-mount watchdog
directly so the SMB share folders do not stay empty or stale after source
volumes are unlocked:

```bash
sudo ./scripts/setup_share_bind_mount_watch.sh --install-systemd
```

The timer checks the repo-managed `/etc/fstab` bind targets every 5 minutes by
default and remounts targets whose source directory is available but whose
share target is missing or still points at the pre-unlock directory.
The current upstream File Browser UI does not display recursive folder sizes,
so directory rows show `-` in the size column. Use host-side tools such as
`du -sh` if you need actual folder totals.

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

## Cloud Storage Accounts

Cloud onboarding starts with local configuration proof, not synchronization.
The first phase accepts only `mode = "inventory"`: it verifies that an
owner-only registry maps to aliases and backend types in an encrypted rclone
config, but it never lists cloud objects, contacts a provider, mounts storage,
copies files, or deletes data. Provider OAuth is a separate, explicitly online
permission gate documented in the guide.

Google Drive, OneDrive, and iCloud Drive are onboarded by name, each bound to
one exact rclone backend type. Google Drive and OneDrive authenticate with a
scoped, separately revocable OAuth token and can be enrolled read-only. iCloud
cannot: rclone exposes no scope option for `iclouddrive`, so its grant is
always read/write. rclone also rejects app-specific passwords there and
requires the primary Apple ID password, so that credential unlocks the whole
account and revoking it means changing the account password.

The rclone config encryption password comes from the macOS Login Keychain by
default, or from KeePassXC through the `auto-pass` sibling repo.

```bash
python3 scripts/cloud_accounts.py init
python3 scripts/cloud_accounts.py providers
python3 scripts/cloud_accounts.py validate
# Verify the KeePassXC lookup; prints only the length, never the password:
python3 scripts/rclone_config_password.py --check
# After separately approved provider enrollment:
python3 scripts/cloud_accounts.py doctor --password-source auto-pass
```

See [docs/cloud-storage.md](docs/cloud-storage.md) before configuring a remote.
Two-way sync remains intentionally unsupported until conflict ownership,
deletion quarantine, recovery, and offline-node behavior are explicit. Use
client-encrypted Restic backups through `traction-control` when the goal is
off-site backup rather than phone-visible browsing.

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
