# REFS-PUBLIC.md - Public References

> Record external public repositories, datasets, documentation, APIs, or other
> public resources that this repository utilizes or depends on.
> This file is tracked and intentionally kept free of private or local-only details.

## Public Repositories

- https://github.com/filebrowser/filebrowser - upstream File Browser project used by the optional web UI and fork workflow
- https://github.com/caddyserver/caddy - upstream Caddy project used by the optional HTTPS front end
- https://github.com/rclone/rclone - cloud storage configuration and transport tool used by the inventory-only onboarding flow

## Public Datasets and APIs

- No standing public data APIs are required; the repo serves local file shares and local/private web surfaces.

## Documentation and Specifications

- https://www.samba.org/samba/docs/ - Samba configuration reference for SMB access
- https://www.wireguard.com/ - WireGuard reference for the private-access overlay
- https://tailscale.com/kb - Tailscale documentation for the optional subnet-router configuration
- https://rclone.org/docs/ - rclone configuration, encryption, and password-command reference
- https://github.com/rclone/rclone/releases/tag/v1.75.0 - checksum-pinned rclone release used by the cloud-account CI regression
- https://restic.readthedocs.io/ - client-encrypted backup reference for the separate off-site backup path

## Notes

- Public refs cover the optional network and web stack only. Host share data, local endpoints, and mobileconfig artifacts stay in REFS-LOCAL.
