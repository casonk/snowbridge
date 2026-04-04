# AGENTS.md — snowbridge

> Scope: this file governs agent and contributor behavior for this repository.

Portfolio-wide standards live in `./util-repos/traction-control` from the
portfolio root. This repository is `./util-repos/snowbridge`.

## Repository Purpose

`snowbridge` is the portfolio-standard SMB-based private file-sharing utility
repo and the configuration/operations home for a personal fileshare hosted on a
home desktop.

The current baseline is:

- Samba-first access for cross-device file sharing
- authenticated read/write access for trusted devices, including iPhone via the
  iOS Files app
- share data and Samba credentials stored outside git
- LAN or VPN access only, not direct internet exposure

## Shared Portfolio References

- `./util-repos/traction-control`: portfolio-wide standards and repo baseline
- `./util-repos/archility`: standard architecture bootstrap and render tooling
- `./util-repos/auto-pass`: standard password-management utility repo
- `./util-repos/clockwork`: standard cron and systemd scheduler rendering repo
- `./util-repos/nordility`: standard VPN-switching utility repo
- `./util-repos/shock-relay`: standard external-messaging utility repo
- `./util-repos/short-circuit`: standard WireGuard VPN setup and configuration
- `./util-repos/dyno-lab`: standard unified test bench utility repo
  utility; use it to install and manage the WireGuard tunnel used for remote
  SMB and HTTPS access

## Session Continuity

- Read `AGENTS.md`, `LESSONSLEARNED.md`, and local-only `CHATHISTORY.md` before
  making substantive repo changes.
- Update local-only `CHATHISTORY.md` after meaningful work.
- Add durable operational guidance to `LESSONSLEARNED.md` when a lesson should
  change future behavior.

## Key Files

- `README.md`
- `config/samba/smb.conf.example`
- `config/access/wireguard/`: WireGuard config examples (server and client peer);
  use `./util-repos/short-circuit/scripts/setup_wireguard.sh` to install them
- `docs/host-setup.md`
- `docs/contributor-architecture-blueprint.md`
- `docs/diagrams/repo-architecture.puml`
- `docs/diagrams/repo-architecture.drawio`

## Operating Rules

1. Treat this repo as configuration and documentation, not as the storage
   location for shared files.
2. Never commit passwords, passdb exports, `.tdb` files, VPN secrets, host-only
   firewall exports, or machine-specific private data.
3. Keep committed hostnames, usernames, IPs, and filesystem paths generic
   unless an exact value is required for a safe template.
4. When changing the Samba baseline, update `README.md`,
   `docs/host-setup.md`, and the architecture docs in the same change.
5. Prefer authenticated SMB for iPhone compatibility unless the user explicitly
   asks for a different transport.
6. Do not recommend exposing TCP 445 directly to the public internet. Remote
   access should go through a private VPN or equivalent tunnel.
7. Use Conventional Commits for any git history you create.

Last reviewed: `2026-04-04`
