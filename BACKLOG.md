# BACKLOG.md

Portfolio backlog for this repository. Pending items are candidates for execution —
manually or via crew-chief. Entries sourced from archility audit are tagged
`[archility:YYYY-MM-DD]`; manual entries use `[manual:YYYY-MM-DD]`.

The archility twice-weekly job populates this file automatically via `archility audit --write-backlog`.
To execute a backlog item with crew-chief: `crew-chief agent "Work on item: <item text>"`.
Mark items `[x]` when complete and move them to Done.

## Pending

- [ ] [manual:2026-10-15] **Ask GitHub Support to purge pre-rewrite commits** — this
  repository's published history was rewritten (most recently on 2026-10-15,
  to remove per-commit AI attribution trailers). GitHub keeps serving the old
  commits to anyone with a hash, and through the read-only `refs/pull/*` refs
  of earlier pull requests, until Support garbage-collects them. The request
  is filed once for the whole portfolio from traction-control's backlog; mark
  this done when an old hash returns 404 here. Never push an old clone's
  history back.

- [ ] [manual:2026-10-11] **Ask GitHub Support to purge pre-rewrite history** — `main`
  was rewritten on 2026-10-11 to remove a home public IPv4 address from
  every commit. GitHub still serves the old commits to anyone who has a hash,
  and through the read-only `refs/pull/*` refs of earlier pull requests, until
  Support removes them. Open a ticket at <https://support.github.com/request>
  ("remove sensitive data"): name this repository, give the old and new `main`
  hashes, and ask for the unreachable commits to be garbage-collected, cached
  views cleared, and the affected pull-request refs and diffs removed. The
  hashes are recorded locally, outside git, in
  `~/.local/share/portfolio-backups/history-rewrite-20261011/` on the Fedora
  desktop; do not paste them or the removed values into a public issue. After
  Support confirms, check that an old hash returns 404, then delete the local
  pre-rewrite bundles. Every other clone must re-clone or hard-reset to
  `origin/main` and must not push its old history back.

- [ ] [manual:2026-08-09] Design cloud publication modes after inventory-only
  onboarding: provider permission scope, read-only mesh browsing, one-way copy,
  deletion quarantine, version recovery, conflict ownership, offline-node
  behavior, and restore tests. Do not enable `bisync` or mounts by default.

## In Progress

## Done
