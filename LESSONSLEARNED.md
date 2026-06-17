# LESSONSLEARNED.md — snowbridge

> Purpose: record durable lessons that should change how future agents and
> contributors work in this repository.

## How To Use This File

- Read this file before repeating setup or design work.
- Keep entries concise and reusable.
- Do not use this file as a session log.

## Lessons

- Document the repository around its real execution, curation, or integration flow instead of only the top-level folder list.
- Keep local-only, private, reference-only, or generated boundaries explicit so published or runtime behavior is not confused with offline material or non-committable inputs.
- Re-run repo-appropriate validation after changing generated artifacts, diagrams, workflows, or other CI-facing files so formatting and compatibility issues are caught before push.
- When a repo installer needs dynamic systemd units, render them through the
  shared `./util-repos/clockwork` manifest flow instead of growing another
  inline here-doc block for service and timer text.

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

### 2026-03-28 — Prefer bind mounts over Samba wide links for external folders

- When the SMB share needs to expose folders that live outside the share root,
  bind-mount them underneath `/srv/snowbridge/share` instead of using Samba
  `wide links`.
- Use ACLs to grant the dedicated SMB account access to the source folders while
  leaving the actual data in place on the host.

### 2026-03-28 — Public access should terminate at a separate HTTPS layer

- If browser access is needed, expose a dedicated HTTPS service such as a web UI
  or reverse proxy rather than broadening the Samba exposure boundary.
- Do not treat public HTTPS access as permission to expose TCP 445 directly.

### 2026-03-28 — Guided setup scripts should handle known package prerequisites

- When a repo-managed installer depends on a small set of distro-known runtime
  packages, prefer installing them inside the script instead of failing on a
  missing command and forcing an undocumented manual recovery step.
- Keep the package installation scope tight and conditional so optional
  capabilities only pull optional dependencies.

### 2026-03-28 — Compose-based host installers should not hardcode Docker

- Fedora-class systems often expose Podman through a Docker-compatible CLI, so
  repo installers should detect a supported Compose frontend rather than assume
  `docker compose` works directly.
- When an installer is meant to be host-portable, prefer explicit runtime
  detection and a small package bootstrap path over a single hardcoded container
  command.

### 2026-03-28 — Podman-facing compose templates should use fully qualified image names

- Short image names can resolve through a distro-specific default registry under
  Podman, which can silently redirect pulls away from the intended upstream
  image source.
- For repo templates that should work across Docker and Podman hosts, prefer
  explicit image references such as `docker.io/...` over ambiguous short names.

### 2026-03-28 — Containerized reverse proxies should target service names, not host loopback

- When Caddy and its upstream run in separate compose services, `127.0.0.1`
  inside the Caddy container points back to Caddy itself, not the sibling app
  container.
- Compose-backed reverse-proxy templates should use the peer service name, such
  as `filebrowser:80`, for the upstream target.

### 2026-03-28 — Podman bind mounts need SELinux relabeling on Fedora-class hosts

- A bind-mounted host path can have correct Unix ownership and still fail inside
  a container when SELinux labels remain at a generic host type such as
  `var_lib_t`.
- Podman-facing compose templates should add `:Z` or `:z` on bind mounts when
  the intended default target is a Fedora-class host with SELinux enforcing.

### 2026-03-30 — Container mounts that point at a bind-mounted share root need both recursive bind and host-to-container propagation

- The Snowbridge share root is a parent directory whose visible folders are
  often themselves separate bind mounts from elsewhere on the host.
- Podman short `-v` syntax defaults to a non-recursive bind, and the default
  propagation mode is also `rprivate`; together that can hide nested host
  submounts from the container and make File Browser show only directories that
  physically live in the root such as `tmp`.
- When a container should see bind-mounted children under a mounted parent path,
  use both recursive bind semantics and an explicit host-to-container
  propagation mode such as `rbind,rslave`, and keep any one-shot management
  containers aligned with the same mount behavior.
- Do not self-bind the share root onto itself as a generic fix. On this host
  that overlaid the original child mounts and replaced working share folders
  with empty directories from the root filesystem.

### 2026-03-28 — Non-root containers should not bind privileged ports

- If a service is configured to run as a non-root UID/GID inside the container,
  it should not be expected to bind a privileged port such as `80`.
- In compose-backed reverse-proxy stacks, prefer an unprivileged internal app
  port such as `8080` and let the reverse proxy own the public-facing ports.

### 2026-03-28 — Host setup scripts should expose an explicit recreate path

- Idempotent `up -d` should remain the default for compose-backed installers,
  but the script should also expose a built-in recreate path for mount, label,
  port, or image-definition changes that cannot be applied in place.
- Prefer an explicit flag such as `--recreate` over unconditional container
  removal at the start of every run.

### 2026-03-28 — Host-local HTTPS testing needs both trust and name resolution

- A browser error like "not found" against a local HTTPS hostname is usually a
  host resolution problem, not a TLS problem.
- For private HTTPS setups that use `tls internal`, local desktop browsing
  needs two separate host-side steps: the hostname must resolve locally and the
  generated Caddy root CA must be installed into the host trust store.

### 2026-03-28 — File Browser auth and process identity should be managed declaratively

- File Browser does not reuse Samba credentials automatically; it has its own
  user database and root/scope model.
- The web process UID/GID must also match a host identity that can traverse the
  share root, so user setup should manage both the File Browser database state
  and the runtime UID/GID together instead of relying on ad hoc manual
  one-liners.

### 2026-03-28 — Local credential configs should self-heal obvious quoting mistakes

- Password-heavy local configs are easy to break with shell-style quoting or
  backslashes that are not valid in the target file format.
- When the intended recovery is mechanically clear, prefer normalizing the
  local config automatically and only fail when the line is genuinely
  ambiguous.

### 2026-03-28 — WireGuard peer templates should generate missing key pairs coherently

- The server private key and the iPhone public key form one pair, and the
  iPhone private key and the server public key form the other.
- When both placeholders for a pair are still present, the setup script should
  generate and write a coherent pair automatically instead of failing on a
  missing key that the repo is already in a position to create.

### 2026-03-28 — Setup scripts should treat checked-in sample values as incomplete config

- Generic placeholder detection is not enough when an example uses a realistic
  sample value such as `vpn.example.com:51820`.
- If a sample value would still produce a syntactically valid but unusable
  config, the setup script should fail with a targeted message instead of
  quietly proceeding or exporting a misleading client artifact.
- When there is a safe mechanical fallback, such as substituting the current
  public IP for a still-sample VPN endpoint, the script can apply it
  automatically but should still warn that a stable endpoint is the better
  final state.

### 2026-03-28 — Private VPN web examples should distinguish loopback-only from phone-reachable binds

- A private HTTPS stack that binds Caddy only to `127.0.0.1` is usable for
  host-local testing but not for phones or other VPN clients.
- For iPhone access over WireGuard, prefer serving both a private hostname and
  the tunnel IP, and use a host-reachable bind such as `0.0.0.0` or the
  specific tunnel IP instead of loopback-only listeners.

### 2026-03-30 — iPhone trust setup is more reliable with a configuration profile than a raw cert copy

- A raw `.crt` file on an SMB share is not always a smooth installation path on
  iPhone, even when the certificate itself is valid.
- For private HTTPS trust bootstrapping, prefer generating a `.mobileconfig`
  profile that installs the root certificate payload, then document the
  follow-up `Certificate Trust Settings` step explicitly.

### 2026-03-30 — If a VPN profile advertises DNS, the installer should actually provide it

- Advertising `DNS = 10.99.0.1` in the WireGuard client profile is not enough
  on its own; the repo also needs to configure a resolver on that tunnel
  address.
- For private hostname access such as `files.snowbridge.internal`, prefer a
  small split-DNS helper over teaching clients to browse the raw tunnel IP,
  especially when the HTTPS layer depends on hostname-based certificate
  selection.
- When the host also has a desktop-only `/etc/hosts` override for the same
  private name, the WireGuard DNS helper must ignore `/etc/hosts` or it will
  hand VPN clients a bogus `127.0.0.1` answer alongside the real tunnel IP.
- On firewalld-based systems, the VPN interface itself needs an explicit zone
  assignment; otherwise SMB might work because the default zone allows `samba`
  while private HTTPS and DNS silently fail because that same zone does not
  allow `https` or `dns`.

### 2026-03-30 — Repeated multi-service troubleshooting should have a single capture script

- Private-access failures span WireGuard, DNS, firewall, Samba, and the web
  stack, so ad hoc command lists are slow to repeat and easy to execute
  inconsistently.
- When a failure mode repeatedly needs the same cross-service evidence, add a
  repo debug script that writes one timestamped report under an ignored
  directory such as `reports/`.

### 2026-06-13 — User-run recovery should be distilled to one command

- When a live host fix requires user-run privileged commands, collapse the
  sequence into one copy/button-friendly shell command whenever execution order
  is fixed and failure should stop later steps.
- Prefer a single repo script for repeated workflows; otherwise use one
  `bash -lc` command with `&&` so it can be run from a web terminal button
  without manual command-by-command transcription.

### 2026-05-31 — Browser reachability needs a backend check, not only Caddy checks

- The shared HTTPS edge can stay up and continue requiring mTLS while the local
  File Browser backend on `127.0.0.1:8080` is down.
- For Safari/File Browser reports, probe the backend directly with a real
  `GET`, because File Browser may not answer `HEAD` the same way.
- Keep a lightweight systemd timer around the backend probe so compose can
  restart File Browser when the HTTPS edge is healthy but the app backend is
  not listening.

### 2026-06-13 — Share reachability needs a bind-mount check, not only Samba checks

- Samba can stay active and advertise the `snowbridge` share while the visible
  folders underneath `/srv/snowbridge/share` are missing or still bound to
  pre-unlock source directories.
- Detect stale bind mounts by comparing the source and target directory
  identities, not only by checking whether the target is a mountpoint.
- When validating an existing bind mount against the configured source, compare
  normalized real paths or directory identity, not only the literal path
  string reported by `findmnt`; equivalent aliases such as `/root/mnt/...` and
  `/mnt/...` can refer to the same mounted directory.
- Keep a root-owned timer around the bind-mount probe so managed `/etc/fstab`
  targets are remounted after encrypted/source volumes become available.

### 2026-03-28 — Regenerated SVG diagrams should be normalized before pushing

- Architecture renderers can produce checked-in SVG files without a trailing
  newline even when the visual output is otherwise correct.
- Run the repo's formatting or pre-commit checks after regenerating diagram
  artifacts so `end-of-file-fixer` does not fail later in CI.

### 2026-03-30 — Separate access modes should validate the bind boundary they imply

- A host-local installer mode is not actually separate if the generated config
  silently falls back to the same broad bind behavior as another mode.
- When a mode is meant to bind a service only on a host's private RFC1918
  address, prefer explicit validation and a best-effort default over leaving
  `0.0.0.0` in place and hoping the user narrows it later.

### 2026-03-30 — VPN profile names should describe routing scope, not just transport

- A single generic WireGuard example blurs together two materially different
  setups: "publicly reachable VPN to the host only" and "publicly reachable VPN
  that also routes the wider home LAN".
- Prefer explicit repo profiles for host-only versus wider-LAN WireGuard so the
  intended `AllowedIPs`, forwarding requirements, and firewall expectations are
  visible at the template and installer level.

### 2026-03-30 — Multiple local profiles need distinct local filenames, not just profile flags

- If two profiles share the same ignored local config paths, initializing the
  second profile silently overwrites or mutates the first profile's local state.
- When the repo supports side-by-side profile variants, the default local file
  names should also be profile-specific so users can keep both variants ready at
  once.

### 2026-03-30 — Do not carry legacy Samba `socket options` tuning into share sections

- `socket options` is a global Samba parameter, so putting it inside a share
  section such as `[snowbridge]` only produces warnings like `Global parameter
  socket options found in service section!`.
- For this repo's baseline, prefer leaving `socket options` unset entirely
  unless there is a measured host-specific need to add a global override.

### 2026-03-30 — Private browser mTLS should be a separate VPN-only web mode with its own client CA

- Device-bound web authentication belongs in the optional HTTPS layer, not in
  Samba or in the WireGuard peer definition.
- When private HTTPS adds client-certificate auth, use a dedicated host-local
  client CA and per-device issued identities rather than trying to reuse the
  Caddy server CA or invent a browser flow around hardware identifiers.
- For iPhone installability, export the client identity and the private Caddy
  root CA together as a staged `.mobileconfig`, but keep the signing CA private
  key outside git and outside the SMB share.

### 2026-04-01 — fstab bind mounts over LUKS-backed ext4 paths capture empty btrfs stubs at boot

- The fstab bind-mount block for the snowbridge share runs at boot, before the
  LUKS-encrypted drives are unlocked and their ext4 filesystems are mounted.
- Bind mounts created at that point capture the empty btrfs stub directories
  that sit under the ext4 mount points (e.g. the root btrfs subvolume's
  `/mnt/4tb-m2/read`), not the actual ext4 content.
- After the user unlocks LUKS (via KeePassXC), the bind mounts remain stale —
  `findmnt` reports the source as the btrfs device even though the ext4 content
  is now accessible at the same path.
- `setup_bind_share.py` currently treats the btrfs-subpath form
  `nvme1n1p3[/root/mnt/4tb-m2/read]` as matching the expected source
  `/mnt/4tb-m2/read` via an `endswith` check, so it silently skips the stale
  bind mount without re-creating it.
- Fix: run `sudo bash scripts/remount_luks_share.sh` once per session after
  unlocking LUKS.  The script unmounts the stale binds and re-mounts them from
  the fstab entries, this time going through the live ext4 paths.
- Folders whose sources are on btrfs (e.g. `keepass` at `/home/user/luks`) are
  unaffected because they are accessible at boot and the bind mount is correct.

### 2026-04-03 — Bind-mount source validation should compare canonical `findmnt` identities, not path suffixes

- `findmnt` can describe the same live source directory in multiple valid forms,
  including a plain path, a device plus subpath such as
  `/dev/mapper/setup[/bully/info/receipt]`, and boot-time stale btrfs-source
  forms such as `nvme1n1p3[/root/mnt/4tb-m2/read]`.
- Path-suffix matching is too loose: it can reject a valid live source that is
  expressed relative to the filesystem root, and it can also falsely accept a
  stale pre-unlock bind source that merely ends with the configured path.
- For repo-managed bind mounts, compare the target's current source against the
  configured source's own canonical `findmnt` source identity, with device
  realpath normalization for `device[subpath]` forms.
- Derive that canonical identity from both `findmnt SOURCE` and `findmnt TARGET`
  for the configured source path. The `SOURCE` field alone only names the
  containing mount, while the bind mount itself is recorded with the full
  filesystem-root-relative subpath such as `/dev/nvme1n1p3[/home/user/luks]`.

### 2026-04-03 — iptables MARK cannot bypass NordVPN policy routing for locally-generated packets

- NordVPN (when connected) installs ip rules (priority 32760+) that route all
  non-marked internet traffic through `nordlynx` (routing table 205). WireGuard
  handshake responses to a phone's cellular IP are internet-bound and get
  redirected through NordVPN, so the phone receives a response from the NordVPN
  server IP rather than the desktop's public IP and rejects the handshake.
- The obvious fix — marking outgoing UDP port 51820 packets with NordVPN's
  fwmark `0xe1f1` in `iptables -t mangle OUTPUT` — does NOT work. For locally-
  generated packets the kernel makes its routing decision before the mangle
  OUTPUT hook runs, so the mark is invisible to policy routing.
- When `nordvpn set firewall disabled` is active, NordVPN also does NOT add the
  ip rule `fwmark 0xe1f1 lookup main` (priority 32760), so even if the mark
  were set in time, there would be no rule to act on it.
- The correct fix uses WireGuard's socket-level SO_MARK (`wg set wg0 fwmark
  51820`), which IS present at routing-decision time. Pair it with an ip rule
  at a priority higher than NordVPN's: `ip rule add fwmark 51820 lookup main
  priority 100`. WireGuard's own UDP packets are then routed via the main table
  (real internet gateway / enp5s0) instead of nordlynx.
- `192.168.0.7` (LAN IP) is not reachable from the internet — cellular clients
  must use the WireGuard tunnel IP `10.99.0.1` or a hostname that resolves
  through dnsmasq on the tunnel.

### 2026-03-30 — Private mTLS browser mode should terminate auth at Caddy and proxy the trusted app user

- If the private HTTPS layer already requires a verified client certificate, do
  not leave a second interactive username/password page in front of the same
  app path by default.
- For this repo's File Browser integration, prefer Caddy mTLS plus File Browser
  proxy auth with an injected trusted username header so a valid device
  certificate lands directly in the app while the service remains unreachable
  without the client cert.

### 2026-04-03 — NordVPN's nordlynx interface must be excluded from WireGuard routing restoration

- `wg show interfaces` discovers ALL WireGuard interfaces, including NordVPN's
  own `nordlynx` interface (NordLynx is WireGuard-based).
- Running `wg set nordlynx fwmark <value>` overwrites the socket fwmark that
  NordVPN manages internally (0xe1f1), which can interfere with NordVPN's own
  anti-loop routing mechanism.
- Filter routing restoration to interfaces that have a corresponding config file
  in `/etc/wireguard/` (e.g. `/etc/wireguard/wg0.conf`). VPN-daemon-managed
  interfaces like `nordlynx` have no such file and are safely excluded.
- Peer handshake refresh (`wg set <iface> peer <pubkey> endpoint <ep>`) is safe
  on all discovered interfaces and does not need the same filter.

### 2026-04-04 — Direct-IP WireGuard client profiles need endpoint-drift automation or a stable DNS name

- A phone profile that embeds the home's current public IP in `Endpoint = ...`
  will silently go stale after the ISP changes the WAN address, even though the
  rest of the host-side WireGuard, Samba, and web stack may still be healthy.
- Prefer a stable DNS or DDNS endpoint when possible. If the deployment keeps a
  raw public IP instead, automate three steps together: detect the new public
  IP, rewrite the ignored client profiles, and regenerate the exported QR
  artifacts immediately.
- Notification state should be tracked separately from endpoint-application
  state so a failed email or Signal send is retried on the next run without
  needing the endpoint drift to happen again.

### 2026-04-09 — Classic File Browser does not provide real folder sizes in its UI

- The upstream `filebrowser/filebrowser` frontend hardcodes directory rows to
  show `-` for size, and the backend listing path only carries the directory
  entry's own filesystem size rather than a recursive tree total.
- Treat this as an upstream product limitation, not a `snowbridge` setup bug.
- If operators need actual folder totals, point them at host-side `du -sh` or
  plan a different web UI / custom fork instead of chasing hidden config.

### 2026-04-09 — A custom File Browser fork should have one image override path

- If both the long-running web stack and the one-shot access-management
  container need the same forked File Browser build, drive both from one
  `FILEBROWSER_IMAGE` value in the web env file.
- Keep `runtime.filebrowser_image` as an optional escape hatch, not the default
  source of truth, so operators do not have to remember two image tags.

### 2026-04-09 — The local File Browser fork image needs a static Go binary

- The custom File Browser image uses a BusyBox/musl runtime, so staging a host
  glibc-linked binary can fail at startup with
  `/init.sh: exec: line 35: filebrowser: not found`.
- Build the staged binary with `CGO_ENABLED=0` in
  `scripts/build_filebrowser_fork_image.sh` so the runtime image can execute it.

### 2026-04-09 — Directory-size loading should degrade gracefully on nested permission errors

- On-demand folder-size calculation walks each visible child directory
  recursively.
- A nested filesystem permission error should not turn the whole listing into a
  403 page. Skip unreadable descendants and return the size of the accessible
  contents instead.
