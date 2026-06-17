# Host Setup

This guide describes the intended baseline deployment for `snowbridge`: a
home-desktop Samba share that trusted devices can mount with authenticated
read/write access.

## Assumptions

- The host is a Linux desktop on the home network.
- The actual shared files live outside this repository.
- Access is limited to the LAN or to devices connected through a private VPN.

## 1. Install Samba

Fedora example:

```bash
sudo dnf install samba
```

Debian or Ubuntu example:

```bash
sudo apt update
sudo apt install samba
```

## 2. Create a Dedicated Share Account

Example:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin snowbridge
sudo smbpasswd -a snowbridge
```

If the host uses a different `nologin` path, substitute the distro-appropriate
value.

## 3. Create the Share Root

Example:

```bash
sudo install -d -o snowbridge -g snowbridge -m 2770 /srv/snowbridge/share
```

Keep the share root outside the git repo so shared files and runtime metadata do
not become tracked content. Treat it as a stable mountpoint tree for the SMB
share, not as the canonical storage location for your files.

## 4. Define the Bind-Mounted Share Layout

Use `config/share-layout/folders.example.ini` as the baseline. Adapt
`config/share-layout/folders.local.ini` for the actual host.

Typical workflow:

1. Decide which existing host folders should appear in the share.
2. Map each source folder into a relative target path underneath
   `/srv/snowbridge/share`.
3. Keep the bind-mounted share root generic. The real file contents should stay
   in their existing storage locations.

Edit `config/share-layout/folders.local.ini`:

```ini
[global]
share_root = /srv/snowbridge/share
smb_user = snowbridge
smb_group = snowbridge
acl_user = snowbridge

[folder Documents]
source = /home/user/Documents

[folder Photos]
source = /mnt/storage/photos
target = Media/Photos
```

`target` is relative to `share_root`. The script will ensure the mountpoint
directories exist and then bind-mount each source folder into the corresponding
target path.

## 5. Create the Bind Mounts and ACLs

Run the setup script from the repo root:

```bash
sudo ./scripts/setup_bind_share.py \
  --config config/share-layout/folders.local.ini \
  --write-fstab
```

What it does:

1. Ensures the share root and per-folder mountpoints exist.
2. Applies ACLs for the SMB user to each source folder.
3. Grants traverse-only ACLs on parent directories when configured.
4. Activates each bind mount with `mount --bind`.
5. Writes a managed bind-mount block to `/etc/fstab` when `--write-fstab` is
   supplied.

Useful flags:

- `--dry-run`: print the actions without changing the system
- `--skip-mount`: stage directories and ACLs without activating bind mounts yet
- `--skip-acls`: only create the mountpoint structure and bind mounts

Bind mounts are the recommended path for exposing folders that live outside the
share root. Avoid Samba `wide links` for this use case.

### After Unlocking LUKS Drives

If any of the source folders in `folders.local.ini` live on LUKS-encrypted
drives (mounted under `/mnt/`), the fstab bind mounts created above will have
run at boot **before** those drives were unlocked.  That means they captured
empty btrfs stub directories rather than the actual drive content.

After you unlock your LUKS volumes (e.g. via KeePassXC), run:

```bash
sudo bash scripts/remount_luks_share.sh
```

This unmounts the stale fstab bind mounts and re-mounts them through the
now-live ext4 paths.  You need to run this **once per session** after unlocking
your drives.  File Browser will reflect the correct folder contents on the next
directory listing.

To verify the full managed bind layout, including folders that are missing or
still bound to the pre-unlock directory, run:

```bash
sudo ./scripts/check_share_bind_mounts.sh --repair
```

To keep the share from staying stale after a boot or delayed unlock, install
the root-owned bind-mount watchdog timer:

```bash
sudo ./scripts/setup_share_bind_mount_watch.sh --install-systemd
```

The timer checks every 5 minutes by default. It remounts managed `/etc/fstab`
targets only when the configured source directory exists and the share target
is missing or points at a different directory identity.

Folders whose sources are on btrfs (e.g. a path under `/home/`) are unaffected
and do not need a post-LUKS remount.

## 6. Apply the Samba Configuration

Use `config/samba/smb.conf.example` as the baseline.

Typical workflow:

1. Copy the example into the host Samba config path, typically
   `/etc/samba/smb.conf`.
2. Adjust the share path, workgroup, and optional hostname fields.
3. Keep guest access disabled unless there is a clear reason to change it.

Validate before restarting Samba:

```bash
testparm -s config/samba/smb.conf.example
```

For host validation, run `testparm` against the final host config file instead
of the example file path when appropriate.

## 7. Enable the SMB Service

Fedora example:

```bash
sudo systemctl enable --now smb.service
```

Debian or Ubuntu example:

```bash
sudo systemctl enable --now smbd.service
```

## 8. Restrict Firewall Exposure

Allow SMB only on trusted network boundaries.

Fedora firewalld example:

```bash
sudo firewall-cmd --permanent --add-service=samba
sudo firewall-cmd --reload
```

Do not port-forward TCP 445 to the public internet.

## 9. Connect From iPhone

### Determine the Server Address

Use the host's current hostname or private IP address, not the share path.

Useful commands:

```bash
hostnamectl --static
hostname -I
ip -4 addr show
```

How to choose:

- Prefer the host's local DNS or mDNS name when it resolves correctly on your
  network, for example `smb://snowbridge.local`.
- Fall back to the host's private IPv4 address if name resolution is unreliable,
  for example `smb://192.168.1.50`.
- Do not use the Samba share name as the server address. The iPhone connects to
  the host first and then presents the available shares after authentication.

1. Open the Files app.
2. Choose `Browse`.
3. Tap `...`.
4. Tap `Connect to Server`.
5. Enter `smb://<desktop-hostname-or-ip>`.
6. Authenticate as the dedicated SMB user.

The server address should point at the host, not at the share path. The Files
app will present the available shares after authentication.

Optional convenience automation:

- See `docs/iphone-shortcut.md` for a Shortcuts-based workflow that opens Files
  and prepares the SMB address when you join your home Wi-Fi.

## 10. Use a Stable Address

The server becomes much easier to reach from iPhone and other clients when the
address stays stable.

Preferred approach:

- Create a DHCP reservation in the home router so the server keeps the same
  private address while still using DHCP on the host.

Template:

- `config/network/networkmanager-static-ip.example.sh`

Host-side static IPv4 example with NetworkManager:

```bash
nmcli connection show
sudo nmcli connection modify "<connection-name>" \
  ipv4.addresses "192.168.1.50/24" \
  ipv4.gateway "192.168.1.1" \
  ipv4.dns "192.168.1.1 1.1.1.1" \
  ipv4.method manual
sudo nmcli connection up "<connection-name>"
```

After changing the address, reconnect from iPhone using the new hostname or IP.

## 11. Remote Access Through a Private VPN

If the share needs to be reachable away from home, use a private VPN or similar
tunnel first.

Good baseline rule:

- LAN or VPN: allowed
- direct internet SMB exposure: not allowed

Practical options:

- Install a VPN client directly on the `snowbridge` host and on the iPhone, then
  connect to the host through the VPN-assigned name or address.
- Use a VPN subnet router or equivalent gateway if the iPhone cannot reach the
  LAN subnet directly through the tunnel.
- Keep the firewall scoped to the LAN or VPN boundary even when remote access is
  enabled.

Examples of private-VPN patterns:

- a WireGuard tunnel that exposes only the host's tunnel IP and private web UI
- a WireGuard tunnel that routes the wider home subnet
- a mesh VPN such as Tailscale, either by installing the client on the host
  itself or by advertising the home subnet through a subnet router

Templates:

- `config/access/tailscale/tailscale-subnet-router.example.sh`
- `config/access/wireguard/wg0-server.example.conf`
- `config/access/wireguard/iphone-peer.example.conf`
- `config/access/wireguard/wg0-server.lan-vpn.example.conf`
- `config/access/wireguard/iphone-peer.lan-vpn.example.conf`

WireGuard setup and installation is handled by `./util-repos/short-circuit`.
Use its `setup_wireguard.sh` installer with the snowbridge config files as
explicit inputs:

WireGuard profiles:

- `wireguard-public-vpn`: publicly reachable WireGuard UDP endpoint, but SMB
  and the optional private web UI stay reachable only after VPN auth on the
  host's tunnel IP or private hostname
- `wireguard-lan-vpn`: same public WireGuard endpoint, but the client also
  routes the wider home LAN through the tunnel

Example host-only WireGuard flow (from the `short-circuit` repo root):

```bash
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

Example wider-LAN WireGuard flow:

```bash
./scripts/setup_wireguard.sh \
  --init-local-configs \
  --profile wireguard-lan-vpn \
  --lan-subnet 192.168.0.0/24 \
  --server-config /path/to/snowbridge/config/access/wireguard/wg0-server.lan-vpn.local.conf \
  --client-config /path/to/snowbridge/config/access/wireguard/iphone-peer.lan-vpn.local.conf

sudo ./scripts/setup_wireguard.sh \
  --profile wireguard-lan-vpn \
  --lan-subnet 192.168.0.0/24 \
  --enable-ip-forward \
  --server-config /path/to/snowbridge/config/access/wireguard/wg0-server.lan-vpn.local.conf \
  --client-config /path/to/snowbridge/config/access/wireguard/iphone-peer.lan-vpn.local.conf \
  --dns-hostname files.snowbridge.internal \
  --print-client-qr
```

The installer will install missing `wireguard-tools` and `dnsmasq` automatically
when a supported package manager is present. If the local configs still contain
paired key placeholders, it generates a matching key pair automatically. If the
client peer `Endpoint` is still on the sample value, it replaces it with the
current public IP and warns you to move to a stable DNS name. For
`wireguard-lan-vpn`, pass `--lan-subnet <cidr>` so it fills the route into the
client peer profile before validation.

If the client profiles keep a raw public IP instead of stable DNS, install the
endpoint-drift monitor so a WAN-IP change rewrites the local peer configs,
regenerates the QR PNGs, and sends the latest endpoint through both email and
Signal:

```bash
./scripts/setup_wireguard_endpoint_monitor.sh --init-local-configs
# edit config/access/wireguard/endpoint-monitor.local.toml
python3 ./scripts/check_wireguard_endpoint.py --dry-run
sudo ./scripts/setup_wireguard_endpoint_monitor.sh --install-systemd
```

The monitor config is local-only and should point at the sibling
`shock-relay/services/gmail-imap/config.local.yaml` and
`shock-relay/services/signal-cli/config.local.yaml` files when those channels
are enabled. The installed timer runs every 15 minutes by default.
The installer renders the systemd unit files through the shared `clockwork`
repo so the scheduler text stays aligned with the portfolio-wide pattern.
If you later switch the WireGuard client profiles to a stable DNS or DDNS
endpoint, remove or disable the timer because there is no longer any direct-IP
drift to correct.

See `./util-repos/short-circuit/docs/setup-guide.md` for the full walkthrough.

## 12. Optional Web Access

If you need browser-based access, add a separate HTTPS front end. Do not expose
Samba itself to the public web.

Safer web-access pattern:

1. Keep Samba on the LAN or VPN only.
2. Add a separate web application or reverse-proxied file UI for browser access.
3. Terminate HTTPS at the web layer, not at Samba.
4. Require authentication, keep the exposed paths minimal, and patch the web
   stack independently of Samba.

Recommended default:

- private HTTPS access behind the same VPN used for SMB

Stronger private-device option:

- private HTTPS behind that same VPN, plus Caddy mutual TLS with per-device
  client certificates

Higher-risk option:

- public HTTPS access with a reverse proxy, strong authentication, TLS, logging,
  and regular updates
- if the host sits behind a home router/NAT, a separate public mode can bind
  Caddy only on the host's private RFC1918 address while the router forwards
  public `80/443` to that private address

Templates:

- `config/web/filebrowser/docker-compose.example.yml`
- `config/web/filebrowser/filebrowser.env.example`
- `config/web/filebrowser/access.example.toml`
- `config/web/caddy/Caddyfile.private-vpn.example`
- `config/web/caddy/Caddyfile.private-vpn-mtls.example`
- `config/web/caddy/Caddyfile.public.example`
- `config/web/caddy/Caddyfile.public-private-ip.example`
- `scripts/setup_caddy_filebrowser.sh`
- `scripts/check_filebrowser_backend.sh`
- `scripts/setup_filebrowser_backend_watch.sh`
- `scripts/setup_filebrowser_access.py`
- `scripts/export_caddy_root_profile.py`
- `scripts/export_caddy_mtls_profile.py`

Example private-VPN web flow:

```bash
./scripts/setup_caddy_filebrowser.sh --init-local-configs --mode private-vpn
./scripts/setup_filebrowser_access.py --init-local-configs
sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn
sudo ./scripts/setup_filebrowser_access.py
```

If you change bind mounts, labels, ports, or image definitions later, rerun it
with `--recreate` so the containers are rebuilt from the updated definition:

```bash
sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn --recreate
```

If Caddy is reachable but Safari cannot load File Browser, verify the local
backend first. A healthy backend returns the File Browser HTML UI on a `GET`
request:

```bash
./scripts/check_filebrowser_backend.sh --url http://127.0.0.1:8080/
```

To prevent the backend from staying down after boot, container restarts, or
Podman state changes, install the root-owned watchdog timer:

```bash
sudo ./scripts/setup_filebrowser_backend_watch.sh --install-systemd
```

The timer checks the local backend every 5 minutes by default and runs the
configured compose service when the probe fails. It does not refresh LUKS bind
mounts, so keep `scripts/start_snowbridge.sh` in the post-unlock flow or
install `scripts/setup_share_bind_mount_watch.sh` as the bind-mount watchdog.

If you want to browse the private HTTPS endpoint from the desktop host itself,
bootstrap the local hostname mapping and install Caddy's local root CA into the
host trust store:

```bash
sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn --bootstrap-local-browser
```

This fixes two separate local-browser prerequisites:

- the configured hostname such as `files.snowbridge.internal` must resolve on
  the host, usually through a hosts entry or local DNS
- the host browser must trust Caddy's internal CA if the site uses
  `tls internal`

Example private-VPN mTLS web flow:

```bash
./scripts/setup_caddy_filebrowser.sh --init-local-configs --mode private-vpn-mtls
./scripts/setup_filebrowser_access.py --init-local-configs
sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn-mtls
sudo ./scripts/setup_filebrowser_access.py
sudo ./scripts/export_caddy_mtls_profile.py --device-name iphone
```

This mode keeps the service inside the same private VPN boundary as SMB, but it
also requires a client certificate signed by a host-local mTLS client CA before
the browser can reach File Browser at all. The web-setup script generates that
client CA automatically under `CADDY_DATA_DIR/mtls/` when the mode is first
applied, and the mTLS export script issues a per-device identity and packages
it with the private Caddy root CA into an Apple `.mobileconfig`.
In this mode the File Browser access script also switches File Browser to proxy
auth automatically, and the Caddyfile injects the trusted app username
`snowbridge`, so a valid client certificate lands directly in the file UI
without a second username/password prompt.

On the iPhone:

1. Open the generated `snowbridge-caddy-mtls-<device>.mobileconfig` from the
   SMB share in Files.
2. Allow the profile download if prompted.
3. Open Settings and tap `Profile Downloaded`.
4. Install the profile and, when prompted, enter the identity password printed
   by `export_caddy_mtls_profile.py`.
5. Go to `Settings > General > About > Certificate Trust Settings`.
6. Enable full trust for the Snowbridge Caddy root certificate.

The web-stack setup script will install a supported local container runtime and
Compose frontend when they are missing. On Fedora-class systems it prefers
`podman` plus `podman-compose`. On Debian or Ubuntu systems without Podman it
uses Docker plus the Compose plugin.

For iPhone access behind WireGuard, prefer the private hostname served by
Caddy, for example `https://files.snowbridge.internal`. The WireGuard setup
script now installs a small split-DNS resolver so that hostname resolves to the
host's tunnel IP for VPN clients. The raw tunnel IP can still be useful for SMB
or debugging, but it should not be the primary browser URL for the private web
path.

If the iPhone does not reliably install or trust the raw `root.crt` file, build
an Apple configuration profile and stage it into the SMB share instead:

```bash
sudo ./scripts/export_caddy_root_profile.py
```

That generates `snowbridge-caddy-local-root.mobileconfig` in
`/srv/snowbridge/share/tmp/` by default, alongside a copy of the raw
certificate. On the iPhone:

1. Open `snowbridge-caddy-local-root.mobileconfig` from the SMB share in
   Files.
2. Allow the profile download if prompted.
3. Open Settings and tap `Profile Downloaded`.
4. Install the profile.
5. Go to `Settings > General > About > Certificate Trust Settings`.
6. Enable full trust for the Snowbridge Caddy root certificate.

For the stronger private-device mTLS path, use the dedicated exporter instead:

```bash
sudo ./scripts/export_caddy_mtls_profile.py --device-name iphone
```

That stages a device-specific `.mobileconfig` plus a fallback `.p12` identity
bundle in `/srv/snowbridge/share/tmp/` by default and prints the import
password you will need during installation.

On SELinux-enforcing hosts such as Fedora, the compose template uses SELinux
relabeling for the mounted host paths.
The File Browser container also listens on unprivileged port `8080` inside the
container so it can run as a non-root UID/GID.

Example public-NAT web flow:

```bash
./scripts/setup_caddy_filebrowser.sh --init-local-configs --mode public-private-ip
./scripts/setup_filebrowser_access.py --init-local-configs
sudo ./scripts/setup_caddy_filebrowser.sh --mode public-private-ip
sudo ./scripts/setup_filebrowser_access.py
```

For `--mode public-private-ip`, set `CADDY_HTTP_BIND` and `CADDY_HTTPS_BIND` in
`config/web/filebrowser/filebrowser.env.local` to the host's private RFC1918
address, for example `192.168.1.10`, not `0.0.0.0`. Then forward public
`80/443` on the router to that same private host IP. This still does not expose
SMB itself; only the separate HTTPS layer should be port-forwarded.
The File Browser access script applies the app root and user database from a
local TOML config, and it can sync the runtime UID/GID in
`filebrowser.env.local` to the configured host account before recreating the
stack.
If you need a custom File Browser fork, set `FILEBROWSER_IMAGE=` in
`filebrowser.env.local` and leave `runtime.filebrowser_image` unset in
`access.local.toml` so both the compose service and the one-shot access
container use the same image tag. The simplest local helper flow is
`./scripts/setup_filebrowser_fork_workspace.sh` followed by
`./scripts/deploy_filebrowser_fork_image.sh`; use
`./scripts/build_filebrowser_fork_image.sh` only when you want to build/tag the
image without recreating the stack yet.
If a password line in `access.local.toml` is pasted in a shell-friendly form
that is not TOML-safe yet, the script will normalize fixable cases
automatically.

If the iPhone still cannot reach the private HTTPS endpoint after WireGuard,
split DNS, and CA trust are in place, collect a single host-side report with:

```bash
sudo ./scripts/debug_private_access.sh
```

The script writes a timestamped report under `reports/` covering WireGuard,
dnsmasq, firewalld, Samba, container status, port listeners, hostname
resolution, and the relevant HTTPS probes.

Do not port-forward TCP 445 as part of any web-access design.

See `docs/access-patterns.md` for the concrete template inventory and when to
choose each option.

## Operational Notes

- A single dedicated SMB account is the simplest starting point for a personal
  multi-device share.
- `force user = snowbridge` in the example config keeps file ownership
  predictable for a single-user share.
- When the source folders live under `/home` or another restricted parent tree,
  the SMB user also needs execute-only traverse access on the parent
  directories.
- The setup script uses ACLs to preserve existing ownership on the source
  folders rather than rehoming the data under `/srv/snowbridge/share`.
- A DHCP reservation is usually the simplest way to keep the server reachable at
  a stable private address.
- If you add browser-based access later, keep it as a separate HTTPS surface
  instead of broadening the Samba exposure boundary.
- The current upstream File Browser UI does not show recursive folder sizes;
  directories display `-` in the size column. Use `du -sh` on the host if you
  need actual folder totals.
- If per-user attribution becomes important later, remove `force user` and
  redesign the share permissions accordingly.
