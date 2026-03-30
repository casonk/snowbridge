# Changelog

All notable changes to `snowbridge` are documented here.

## Unreleased

- Initialized `snowbridge` as a personal fileshare utility repo.
- Added the baseline governance and contributor files used across the
  portfolio.
- Documented a Samba-first access model for authenticated cross-device sharing.
- Added host setup notes and architecture docs for a home-desktop SMB share with
  iPhone compatibility.
- Added a bind-mounted share layout workflow, including config templates and a
  host setup script for ACLs, bind mounts, and `/etc/fstab` management.
- Added optional access templates for stable private addressing, Tailscale,
  WireGuard, and separate HTTPS web access patterns.
- Updated the WireGuard setup script to install missing runtime packages such as
  `wireguard-tools` and optional `qrencode` automatically.
- Updated the web stack installer to bootstrap a supported container runtime and
  Compose frontend automatically, and corrected the example Caddy port
  publishing syntax.
- Updated the web stack compose template to use fully qualified container image
  names so Podman does not resolve them through the wrong registry.
- Corrected the Caddy templates to proxy to the `filebrowser` service across the
  compose network instead of loopback inside the Caddy container.
- Updated the web stack compose template to add SELinux-friendly `:Z` bind mount
  relabeling for Podman on Fedora-class hosts.
- Updated the web stack templates so File Browser listens on unprivileged
  container port `8080` and Caddy proxies to that port.
- Added `--recreate` to the web stack setup script so changed container
  definitions can be rebuilt without manual `podman rm` cleanup.
- Added `--bootstrap-local-browser` to the web stack setup script so the host
  can trust Caddy's internal CA and resolve the configured local HTTPS hostname.
- Added a declarative File Browser access config and setup script for root,
  users, and runtime UID/GID sync.
- Updated the File Browser access script to auto-normalize fixable password
  lines in the local TOML config before parsing.
- Updated the WireGuard setup script to auto-generate missing server and iPhone
  key pairs when the paired placeholders are still present.
- Updated the WireGuard iPhone peer template and installer to treat the sample
  endpoint as incomplete config and to render QR output before attempting the
  `wg-quick` start path.
- Updated the WireGuard installer to auto-fill the iPhone peer endpoint from
  the current public IP when the sample endpoint is still present, while
  printing a warning that the value should be replaced with a stable endpoint.
- Updated the private File Browser HTTPS examples to serve both a private
  hostname and the default WireGuard tunnel IP, and clarified that VPN-phone
  access needs a host-reachable Caddy bind instead of loopback-only listeners.
- Added a profile-export script that converts Caddy's local root certificate
  into an iPhone-installable `.mobileconfig` and stages it into the SMB share.
- Updated the WireGuard installer to set up a dnsmasq-based split-DNS helper so
  `files.snowbridge.internal` resolves on WireGuard clients by default.
- Reverted the private HTTPS example from dual hostname-plus-IP serving back to
  hostname-only serving, because the browser path should use VPN DNS instead of
  raw-IP TLS.
- Added a private-access debug script that collects WireGuard, dnsmasq,
  firewalld, Samba, Caddy, and File Browser state into a single report under
  `reports/`.
- Updated the WireGuard installer to prevent `/etc/hosts` from leaking extra
  A records into the WireGuard DNS response and to map the WireGuard interface
  into a firewalld zone that permits private VPN traffic.
- Updated the File Browser share-root mount to use `rbind,rslave` so the web
  container can see bind-mounted folders underneath `/srv/snowbridge/share`,
  and aligned the one-shot access-management container with the same runtime
  behavior.
