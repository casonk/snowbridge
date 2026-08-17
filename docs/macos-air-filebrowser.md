# Air Rootless File Browser Backend

The temporary Air primary runs File Browser as a rootless Podman container and
publishes it only on `127.0.0.1:8080`. Wiring Harness owns the separate
WireGuard-facing Caddy listener on `10.99.0.254:8444` and must require mTLS
before proxying a request to this backend.

This path is independent from native macOS File Sharing. It does not inspect or
change SMB, the existing Public Folder, PF, WireGuard, or a macOS password.

## Security Contract

The local config and manager enforce all of these conditions:

- the ignored local TOML is a singly linked, current-user-owned file with mode
  `0600` or stricter;
- the share and dedicated state directories are real, current-UID/GID-owned
  directories with exact mode `0700` and no extended ACL;
- Podman machine and connection names are both explicit and identical, so no
  operation uses the ambient default connection;
- the image is the official `docker.io/filebrowser/filebrowser` repository and
  is selected by a complete SHA-256 manifest digest, never a mutable tag;
- the host publish is exactly `127.0.0.1:8080:8080/tcp`; a public, LAN, or
  WireGuard-address bind is rejected;
- the container runs as the current UID/GID with an explicit `keep-id` UID/GID
  mapping, drops every
  capability, enables `no-new-privileges`, uses a read-only root filesystem,
  and receives only a constrained `/tmp` tmpfs plus its three explicit bind
  mounts;
- File Browser command execution, signup, external-symlink following, admin,
  and share-link permissions are disabled;
- File Browser uses proxy authentication with the fixed
  `X-Snowbridge-Auth-User` header. It auto-provisions the fixed `snowbridge`
  identity with CRUD/download permissions and no admin or command-execution
  permission, so bootstrap needs no application password.

File Browser warns that proxy auth blindly trusts its configured header. The
Wiring Harness Snowbridge role must therefore overwrite any client-provided
value with:

```caddyfile
header_up X-Snowbridge-Auth-User "snowbridge"
```

Do not make that header configurable and do not activate the edge until its
regression proves the overwrite. The backend's loopback-only bind is the second
part of this trust boundary.

## Local Configuration and Review

Initialize once if the ignored local file does not already exist:

```bash
python3 scripts/macos_filebrowser_podman.py init
chmod 600 config/macos/air-filebrowser.local.toml
```

Then validate and render inert review material:

```bash
python3 scripts/macos_filebrowser_podman.py validate
python3 scripts/macos_filebrowser_podman.py render
```

The owner-only output under `artifacts/macos-air-filebrowser/` contains:

- `manifest.json`, including the activation and trust boundaries;
- `container-spec.json`, including the exact bind, mounts, identity, image,
  and security settings;
- `io.github.casonk.snowbridge.air-filebrowser.plist`, the inert user
  LaunchAgent that invokes the manager's `serve` command at login.

Rendering neither starts Podman nor changes any service.

## Explicit Bootstrap

After reviewing both the Snowbridge bundle and the Wiring Harness proxy-header
change, bootstrap as the login user without `sudo`:

```bash
python3 scripts/macos_filebrowser_podman.py bootstrap
```

The idempotent bootstrap creates missing share/state directories as `0700`,
starts only the configured rootless Podman machine, pulls the digest-pinned
image only when absent, initializes or reconciles the dedicated database,
reconciles only a container carrying the Snowbridge managed label, verifies
the loopback health endpoint, and installs the user LaunchAgent at:

```text
~/Library/LaunchAgents/io.github.casonk.snowbridge.air-filebrowser.plist
```

At future logins, that LaunchAgent starts the same named machine and container
with `--pull=never`, then remains alive as their unprivileged supervisor. Every
five seconds it revalidates the Snowbridge managed label, reviewed spec hash,
running state, exact loopback publish, and `/health` response. This persistent
parent also keeps the macOS Podman VM lifecycle attached to the launchd job.

The container itself has no automatic restart policy. A runtime or health loss
stops the labeled container where Podman remains reachable and makes the
manager exit unsuccessfully; launchd then starts a fresh validation cycle. A
SIGTERM or SIGINT wakes the supervisor immediately, stops only that labeled
container, and exits cleanly. A stale spec stops the login runner and requires
another explicit bootstrap; login never silently replaces or downloads it.

Bootstrap captures subprocess output and never prints database contents,
tokens, passwords, or container inspection documents. The managed proxy flow
does not require locally supplied password material.

## Status and Handoff to the Edge

Read status without starting a stopped machine:

```bash
python3 scripts/macos_filebrowser_podman.py status
```

Exit code `0` means the owner-only paths, digest image, managed container,
exact loopback publish, health endpoint, installed LaunchAgent, and loaded
launchd job are all ready. Exit code `3` means the report is valid but at least
one readiness condition is false.

Only after this status is ready should Traction Control enable the optional
Snowbridge backend and Wiring Harness render/activate `10.99.0.254:8444`.
Neither step makes port `8080` directly reachable from the mesh.

The pinned baseline is the
[File Browser v2.63.23 upstream release](https://github.com/filebrowser/filebrowser/releases/tag/v2.63.23).
Its upstream project announced that this is its final planned release, so
adopting another backend or a newer maintained fork requires a separate
image/source and migration review rather than changing this digest in place
without testing.
