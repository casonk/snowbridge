# Cloud Storage Account Onboarding

Snowbridge owns phone-visible cloud account integration. The first supported
phase is deliberately inventory-only: it proves that an owner-only declaration
matches aliases and backend types in an encrypted rclone configuration without
listing remote objects or contacting a provider. It does not mount, copy, sync,
delete, or publish cloud content.

Provider enrollment is a separate online permission gate. Running rclone's
interactive provider configuration can open a browser, contact the provider,
and request scopes that permit writes or deletions. Review that provider's
consent screen and select the least privilege compatible with the intended
later use before approving it.

## Security boundary

- Real aliases and local paths live only in ignored
  `config/cloud/accounts.local.toml`.
- OAuth refresh tokens and provider credentials live only in an encrypted
  rclone config outside the Git checkout.
- On macOS, keep the rclone-config encryption password in Login Keychain and
  let rclone retrieve it through `--password-command`. Do not put it in an
  environment variable, command argument, tracked file, chat, or terminal log.
- `inventory` is the only accepted mode. Two-way sync, mounts, deletions, and
  phone-visible publication need a separate conflict, recovery, and access
  review; rclone is not an ACID transaction coordinator.
- `init` and `validate` are local-only. The doctor's rclone operations are
  backend-offline: it runs only `config encryption check` and
  `listremotes --json`, which do not contact a cloud backend or read remote
  filenames. The fixed macOS Keychain helper is local; an explicitly supplied
  custom password helper has its own I/O boundary and must be reviewed.
- The doctor binds each enabled alias to its declared rclone `backend` type.
  It cannot prove provider reachability, OAuth scope, or that the selected
  remote root exists.

## Initialize local state

Install rclone, then create the empty owner-only registry:

```bash
python3 scripts/cloud_accounts.py init
python3 scripts/cloud_accounts.py validate
```

The default encrypted config path is outside the repository at
`~/.config/snowbridge/rclone.conf`. The initializer creates only the ignored
registry; it does not create a Keychain item or rclone config.

The initialized registry contains `accounts = []`. When adding the first
account, replace that line with one or more `[[accounts]]` tables; do not append
an array-of-tables after it, because that would be invalid TOML. The tracked
example shows the table shape but must never receive live account metadata.

## Create the macOS Keychain secret

First verify that the fixed item does not already exist:

```bash
/usr/bin/security find-generic-password \
  -a snowbridge -s snowbridge-rclone-config >/dev/null
```

If that command reports no item, create it interactively. Keep `-w` last so
macOS prompts without placing the password in process arguments:

```bash
/usr/bin/security add-generic-password \
  -a snowbridge -s snowbridge-rclone-config -w
```

Use one newly generated, high-entropy password and store its recovery copy in
the portfolio password manager. Losing it makes the encrypted rclone config
unrecoverable.

## Encrypt the local rclone config

Set the config encryption before adding an account:

```bash
install -d -m 0700 "$HOME/.config/snowbridge"
rclone \
  --config "$HOME/.config/snowbridge/rclone.conf" \
  --password-command "/usr/bin/security find-generic-password -a snowbridge -s snowbridge-rclone-config -w" \
  config encryption set
```

This encryption step is local-only.

## Online provider enrollment

Stop here until the provider, intended folder, and acceptable permission scope
have been chosen. Then start rclone's provider-specific flow from a private
terminal. This command is online and may grant write or deletion permissions:

```bash
rclone \
  --config "$HOME/.config/snowbridge/rclone.conf" \
  --password-command "/usr/bin/security find-generic-password -a snowbridge -s snowbridge-rclone-config -w" \
  config
```

Do not paste OAuth codes, tokens, client secrets, account email addresses, or
the config password into Git or chat. Give each remote a non-identifying alias
matching `[a-z][a-z0-9-]{0,31}`.

After enrollment completes, inspect the local alias and rclone backend type
without accessing the provider:

```bash
rclone \
  --config "$HOME/.config/snowbridge/rclone.conf" \
  --password-command "/usr/bin/security find-generic-password -a snowbridge -s snowbridge-rclone-config -w" \
  listremotes --long
```

Replace `accounts = []` in the ignored local registry with a matching table.
The `backend` value is the exact rclone type (for example, `drive`, `dropbox`,
`onedrive`, `s3`, or `sftp`), not a display name. Keep `enabled = false` until
the backend, non-empty selected root, intended later behavior, and provider
consent grant are reviewed. Then set it to true, validate, and run the offline
doctor:

```bash
python3 scripts/cloud_accounts.py validate
python3 scripts/cloud_accounts.py doctor
```

The doctor reports only counts. A successful result proves local configuration
encryption plus alias/backend binding. It does not prove provider reachability,
OAuth permission scope, selected-root existence, or suitability for publication.

## Next gate

Before any account becomes phone-visible, choose one behavior per account:

- native provider app only;
- read-only browse through the mesh;
- one-way copy with retained versions; or
- client-encrypted backup through Restic.

Two-way sync remains unsupported until conflict ownership, deletion quarantine,
version recovery, offline-node behavior, and restore testing are explicit.
