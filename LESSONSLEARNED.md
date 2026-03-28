# LESSONSLEARNED.md — snowbridge

> Purpose: record durable lessons that should change how future agents and
> contributors work in this repository.

## How To Use This File

- Read this file before repeating setup or design work.
- Keep entries concise and reusable.
- Do not use this file as a session log.

## Lessons

### 2026-03-28 — iPhone read/write access should target authenticated SMB first

- The native iOS Files app can mount SMB shares directly, so authenticated SMB
  is the lowest-friction baseline for cross-device access.
- Prefer SMB over ad hoc HTTP or custom sync tooling unless the user has a
  different explicit requirement.

### 2026-03-28 — Share contents and Samba secrets must stay outside git

- The repo should version-control configuration and operations guidance, not the
  shared files themselves.
- Keep the actual share root, Samba passdb state, and any credentials outside
  the repository and out of commits.

### 2026-03-28 — Remote access should go through a VPN, not a public SMB port

- Do not expose SMB directly to the public internet.
- When remote access is needed, route it through a private VPN or equivalent
  tunnel first.

### 2026-03-28 — Regenerated SVG diagrams should be normalized before pushing

- Architecture renderers can produce checked-in SVG files without a trailing
  newline even when the visual output is otherwise correct.
- Run the repo's formatting or pre-commit checks after regenerating diagram
  artifacts so `end-of-file-fixer` does not fail later in CI.
