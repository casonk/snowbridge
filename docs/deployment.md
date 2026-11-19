# Deployment modes: snowbridge-files

The File Browser service runs three ways, chosen with one setting, and only the `k3s` mode needs a cluster.
The contract is traction-control's `docs/deployment-modes.md`; this file records what is specific to File Browser.

| Mode | What it is | Needs |
|---|---|---|
| `dev` (default) | the fork's binary in the foreground on 127.0.0.1, with a scratch database and an empty root | the built fork binary |
| `host` | the existing rootful compose container, started and stopped by a user unit | the unit and one sudoers rule (below) |
| `k3s` | a pod in K3s behind a loopback forwarder, so Caddy keeps using `127.0.0.1:8080` | K3s, podman, kubectl, the in-cluster registry |

Nothing here changes File Browser's code: the pod differs only by configuration.

## Choose a mode

```bash
deploy/deploy.sh mode                  # print the mode in effect and where it came from
deploy/deploy.sh up | down | status | verify
```

First match wins: `--mode`, then `SNOWBRIDGE_FILES_DEPLOY_MODE` in the environment, then in `config/deploy.local.env`
(copy `config/deploy.example.env`; gitignored), then `dev`. An unknown value or an unreadable config is an error.

## What each mode does

### dev
Runs the fork in the foreground on `127.0.0.1:8080` against `$TMPDIR/snowbridge-files-dev`. It never touches the real
share or database. Build the fork first: `(cd vendor/filebrowser-upstream && go build -o filebrowser .)`.

### host
The host copy is the rootful compose container that `scripts/start_snowbridge.sh` and the backend watchdog already
manage. Root's podman owns it, so a user unit cannot control it directly. Install two things once:

```bash
deploy/host/install-host-unit.sh          # renders the user unit for this machine; prints the sudoers command
# run the printed command: it installs one rule allowing exactly `podman-compose ... start filebrowser` and `... stop filebrowser`
```

`deploy/host/install-host-unit.sh --print` shows both files without installing anything. The scripts never call
`sudo`; the rule is what lets the user unit start and stop that one container.

### k3s
`up` checks kubectl, podman and the registry first and changes nothing if one is missing. Then, in order:

1. `prepare-host.sh` checks that every folder the pod mounts exists and carries one SELinux label a pod can use.
2. `build-image.sh` builds the fork image from **committed** fork source and pushes it to the in-cluster registry.
3. `apply.sh` creates the namespace, the volumes, the network policy and the workload.
4. `verify.sh` must pass (see below).
5. `bridge.sh on` stops and disables the host unit, then forwards `127.0.0.1:8080` to the pod. If that fails the
   host unit is put back.

## How the pod is wired

- **Volumes.** The pod mounts each *real source folder* of the share (read from the gitignored
  `config/share-layout/folders.local.ini`) at its target under `/srv`, plus the share root, the database and the config
  directory (read-only). It does **not** use the bind-mount aliases: a local volume is a non-recursive bind mount, so
  nested mounts arrive empty and a watchdog remount never reaches the pod. Tested on a scratch layout.
- **A locked drive fails closed.** A source folder that is missing leaves the pod pending instead of writing into a bare
  directory; `prepare-host.sh` and `verify.sh` also fail.
- **SELinux.** The host folders already carry `container_file_t` with one private category pair. `prepare-host.sh` reads
  that level and the pod adopts it (`seLinuxOptions.level`); nothing is relabelled. If the paths disagree it prints the
  `chcon` commands, with `restorecon -RF` to undo. Samba is unaffected (`samba_export_all_rw` is on).
- **Identity.** The pod runs as the database directory's owner (uid and gid read at apply time), read-only root
  filesystem, no capabilities, no service-account token, no `fsGroup`.
- **Network.** Cilium denies all egress; ingress is the node only, on 8080. File Browser needs no outbound connection.
- **Address.** ClusterIP `10.43.0.64`, port 8080 (registered in traction-control's address table).
- **Settings.** `FB_AI_FOLDER_ICONS`, `FB_AI_FOLDER_ICONS_MODEL` and `FB_AI_FOLDER_ICONS_PREFETCH_USER` come from
  `SNOWBRIDGE_FILES_*` keys in `config/deploy.local.env`; absent means off. File Browser's own settings (including the
  branding directory) live in the database and carry over unchanged.

## While the pod serves

A marker (`~/.local/state/snowbridge-files/deploy-mode`) records that the pod holds the port. The boot script
`scripts/start_snowbridge.sh` and the backend watchdog `scripts/check_filebrowser_backend.sh` read it and leave the host
container stopped: recreating it would fight the forwarder for port 8080. The watchdog still probes the port and reports
an unhealthy pod.

## Known limits

- **Folder-icon generation.** The icon service reaches the local model over a unix socket in the config volume. A network
  policy does not cover unix sockets, and SELinux may refuse the connection; `verify.sh` only warns. Cached icons keep
  working; new suggestions may not generate in the pod. Use `--mode host up` to run prefetch.
- **One writer.** The database has a single writer, so the pod uses `Recreate` and the host container must be stopped
  while it runs. `bridge.sh` does that.
- **Existing sessions.** Switching restarts File Browser; browsers reconnect through Caddy.

## Switching

| From | To | Command |
|---|---|---|
| dev | host | stop the foreground process, then `deploy/deploy.sh --mode host up` |
| host | k3s | `deploy/deploy.sh --mode k3s up` (verifies first, then brings the bridge up) |
| k3s | host | `deploy/deploy.sh --mode host up` (takes the bridge down, starts the host container) |

`--mode k3s down` is the same as `--mode host up`; the pod is left in place, stopped from serving. Remove it with
`kubectl delete ns snowbridge-files`. The data never moves.

## verify.sh

Exit 0 all passed, 1 a check failed, 2 nothing could be verified. It checks: the node reaches the service; a burst of 40
concurrent requests; every share folder shows content in the pod; the database is writable and the config is not; the
pod cannot reach the internet; a pod in another namespace cannot reach the service; and (warn only) the model socket.

## For contributors without K3s

`pytest` needs no cluster: the tests parse the manifests and run the scripts against stub commands. Nothing under
`deploy/k3s/` runs unless you select the `k3s` mode. The scripts started from traction-control's
`scripts/scaffold_deployment_modes.py`, and `python3 scripts/check_deployment_modes.py --repo ../snowbridge` (run from
traction-control) audits them against the contract.
