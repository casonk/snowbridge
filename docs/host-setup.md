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

## 10. Remote Access

If the share needs to be reachable away from home, use a private VPN or similar
tunnel first.

Good baseline rule:

- LAN or VPN: allowed
- direct internet SMB exposure: not allowed

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
- If per-user attribution becomes important later, remove `force user` and
  redesign the share permissions accordingly.
