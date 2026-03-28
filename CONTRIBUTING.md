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
- `LESSONSLEARNED.md` is the tracked durable-lessons file.
- `CHATHISTORY.md` is local-only and should stay concise.
- `docs/contributor-architecture-blueprint.md` and `docs/diagrams/` should stay
  aligned with the real operating model.

## Validation

- Run `pre-commit run --all-files` when pre-commit is available.
- Run `testparm -s config/samba/smb.conf.example` or equivalent host-side
  validation when the Samba template changes.

## Pull Requests

- Call out any security or exposure changes explicitly.
- Note whether iPhone access behavior changed.
- Confirm that no secrets, host-local state, or real share data were committed.
