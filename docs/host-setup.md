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

- a WireGuard tunnel that routes the home subnet
- a mesh VPN such as Tailscale, either by installing the client on the host
  itself or by advertising the home subnet through a subnet router

Templates:

- `config/access/tailscale/tailscale-subnet-router.example.sh`
- `config/access/wireguard/wg0-server.example.conf`
- `config/access/wireguard/iphone-peer.example.conf`
- `scripts/setup_wireguard.sh`

Example WireGuard script flow:

```bash
./scripts/setup_wireguard.sh --init-local-configs
sudo ./scripts/setup_wireguard.sh --enable-ip-forward --print-iphone-qr
```

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

Higher-risk option:

- public HTTPS access with a reverse proxy, strong authentication, TLS, logging,
  and regular updates

Templates:

- `config/web/filebrowser/docker-compose.example.yml`
- `config/web/filebrowser/filebrowser.env.example`
- `config/web/caddy/Caddyfile.private-vpn.example`
- `config/web/caddy/Caddyfile.public.example`
- `scripts/setup_caddy_filebrowser.sh`

Example private-VPN web flow:

```bash
./scripts/setup_caddy_filebrowser.sh --init-local-configs --mode private-vpn
sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn
```

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
- If per-user attribution becomes important later, remove `force user` and
  redesign the share permissions accordingly.
