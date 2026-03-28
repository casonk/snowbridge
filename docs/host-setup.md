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
not become tracked content.

## 4. Apply the Samba Configuration

Use `config/samba/smb.conf.example` as the baseline.

Typical workflow:

1. Copy the example into the host Samba config path.
2. Adjust the share path, workgroup, and optional hostname fields.
3. Keep guest access disabled unless there is a clear reason to change it.

Validate before restarting Samba:

```bash
testparm -s config/samba/smb.conf.example
```

For host validation, run `testparm` against the final host config file instead
of the example file path when appropriate.

## 5. Enable the SMB Service

Fedora example:

```bash
sudo systemctl enable --now smb.service
```

Debian or Ubuntu example:

```bash
sudo systemctl enable --now smbd.service
```

## 6. Restrict Firewall Exposure

Allow SMB only on trusted network boundaries.

Fedora firewalld example:

```bash
sudo firewall-cmd --permanent --add-service=samba
sudo firewall-cmd --reload
```

Do not port-forward TCP 445 to the public internet.

## 7. Connect From iPhone

1. Open the Files app.
2. Choose `Browse`.
3. Tap `...`.
4. Tap `Connect to Server`.
5. Enter `smb://<desktop-hostname-or-ip>`.
6. Authenticate as the dedicated SMB user.

The server address should point at the host, not at the share path. The Files
app will present the available shares after authentication.

## 8. Remote Access

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
- If per-user attribution becomes important later, remove `force user` and
  redesign the share permissions accordingly.
