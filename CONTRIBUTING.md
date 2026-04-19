# Contributing

`snowbridge` is a personal fileshare configuration repo.

## Workflow

1. Keep each change scoped to one fileshare concern.
2. Preserve the Samba-first baseline unless the user explicitly chooses a
   different access model.
3. Keep examples generic and portable enough to avoid leaking host-specific
   details.
4. Use Conventional Commits such as `docs: refine iphone setup guidance` or
   `chore: tighten samba template`.

## Content Standards

- `README.md` should stay accurate for the intended deployment path.
- `config/samba/smb.conf.example` should remain a safe baseline and should not
  include secrets.
- `config/share-layout/folders.example.ini` should stay generic and should not
  encode host-specific private paths.
- `LESSONSLEARNED.md` is the tracked durable-lessons file.
- `CHATHISTORY.md` is local-only and should stay concise.
- `docs/contributor-architecture-blueprint.md` and `docs/diagrams/` should stay
  aligned with the real operating model.

## Validation

- Run `pre-commit run --all-files` when pre-commit is available.
- Run `testparm -s config/samba/smb.conf.example` or equivalent host-side
  validation when the Samba template changes.
- Run `python3 scripts/setup_bind_share.py --config config/share-layout/folders.example.ini --dry-run`
  when the bind-mounted share workflow changes.
- Run `./scripts/setup_filebrowser_fork_workspace.sh` when you need to install
  prerequisites or re-run the local checks for `vendor/filebrowser-upstream`.
- Run `./scripts/build_filebrowser_fork_image.sh` when you need a fresh local
  File Browser image tag from the fork workspace for `FILEBROWSER_IMAGE`.
- Run `./scripts/deploy_filebrowser_fork_image.sh` when you want that local
  image tag written into `filebrowser.env.local` and the web stack recreated.

## Pull Requests

- Call out any security or exposure changes explicitly.
- Note whether iPhone access behavior changed.
- Confirm that no secrets, host-local state, or real share data were committed.
