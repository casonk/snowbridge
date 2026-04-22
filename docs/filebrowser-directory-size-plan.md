# File Browser Directory Size Plan

This note captures the current `snowbridge` strategy for adding real folder
sizes to the optional File Browser web UI while keeping the fork scope small
and the upstream proposal clear.

## Verified Current Upstream State

- The classic listing UI renders directory rows as `-` in the size column.
  Source: `frontend/src/components/files/ListingItem.vue`.
- The backend listing path reports the directory entry's own filesystem size
  from `stat`, not a recursive tree total. Source: `files/file.go`.
- The current upstream settings types do not expose a directory-size setting.
  Source: `frontend/src/types/settings.d.ts` and `settings/settings.go`.
- Upstream currently describes the project as maintenance-only with no new
  features planned. That raises the review bar for any feature-shaped PR.

## Snowbridge Fork Workflow

The current local upstream workspace for this effort lives at:

- `vendor/filebrowser-upstream`
- branch: `snowbridge/dirsize`
- current patch commit: `acd3a8d` (`feat: add on-demand directory size loading`)

`snowbridge` now supports a single custom-image override for both:

- the long-running compose service
- the one-shot `setup_filebrowser_access.py` management container

Use this flow:

1. Bootstrap the local fork workspace and run the upstream-style checks:

```bash
./scripts/setup_filebrowser_fork_workspace.sh --install-os-packages
./scripts/setup_filebrowser_fork_workspace.sh
```

2. Build, tag, and deploy a custom File Browser image from that local checkout:

```bash
./scripts/deploy_filebrowser_fork_image.sh
```

3. Leave `runtime.filebrowser_image` unset in
   `config/web/filebrowser/access.local.toml` unless you intentionally want the
   management container to use a different image.
4. Recreate the web stack and re-apply the File Browser state when needed:

```bash
sudo ./scripts/setup_caddy_filebrowser.sh --mode private-vpn --recreate
sudo ./scripts/setup_filebrowser_access.py
```

## Recommended Minimal Fork Scope

Aim for the smallest patch set that can plausibly upstream:

1. Backend:
   add an optional code path that calculates recursive directory sizes only
   when explicitly requested, rather than on every listing by default.
2. API:
   expose the computed directory size in the existing resource payload only
   when that code path is enabled.
3. Frontend:
   render the returned directory size instead of `-` when available.

Avoid broad unrelated cleanup in the fork. Keep the diff focused on the
directory-size flow so rebases stay cheap.

## Upstreamable Design Shape

A reasonable upstream proposal is:

- default behavior stays unchanged to avoid surprise latency on large trees
- add a server setting and/or request-scoped control that permits directory
  size calculation
- optionally add a UI button that requests folder sizes on demand instead of
  forcing recursive scans on every page load

The on-demand button is likely easier to justify upstream because recursive
directory traversal is expensive and the project is maintenance-only.
