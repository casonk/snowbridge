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

## Supported providers

Snowbridge onboards three providers by name. Each maps to one exact rclone
backend type, which is the value an account declares as `backend` and the only
provider binding in the registry — the doctor cross-checks it against the
encrypted rclone config, so a separate provider field could only disagree with
the type rclone actually uses.

| Provider | rclone backend | Credential | Read-only enrollment |
| --- | --- | --- | --- |
| `google-drive` | `drive` | OAuth token | `scope = drive.readonly` |
| `onedrive` | `onedrive` | OAuth token | `access_scopes = Files.Read Files.Read.All Sites.Read.All offline_access` |
| `icloud` | `iclouddrive` | Apple ID + password | none available |

Print the table, including revocation locations and per-provider notes:

```bash
python3 scripts/cloud_accounts.py providers
python3 scripts/cloud_accounts.py providers --json
```

The values above were verified against rclone v1.75.0. Re-verify them with
`rclone help backend <type>` when the installed rclone changes: a renamed or
redefined scope silently grants more access than intended.

### Google Drive and OneDrive

Both authenticate with an OAuth refresh token that is scoped at consent time
and revocable at the provider without changing the account password. Both
default to read/write. Choose the scope during `rclone config`, because
Snowbridge cannot narrow a grant afterwards — only re-enrollment can.

### iCloud Drive

iCloud differs in a way that matters for the threat model, and the difference
is structural rather than a configuration choice:

- rclone exposes no scope option for `iclouddrive`, so enrollment is always
  read/write. Least privilege has to come from the selected root folder.
- The stored secret is an account password, not a scoped token. rclone
  obscures it, and obscuring is reversible encoding, not encryption. Only the
  config encryption protects it at rest, which is why an encrypted rclone
  config is mandatory before any account is added.
- Enroll with an app-specific password generated at
  `account.apple.com` > Sign-In and Security > App-Specific Passwords. That
  keeps the credential individually revocable. Never store the primary Apple
  ID password, whose revocation means changing the account password.
- `service = drive` selects iCloud Drive; `service = photos` selects the photo
  library instead. Confirm which one an account is enrolling.

Confirm the provider's own current terms for this access path before
enrolling. rclone's iCloud support is not a published Apple integration, so
account-protection features may block it or change its behavior.

## Security boundary

- Real aliases and local paths live only in ignored
  `config/cloud/accounts.local.toml`.
- OAuth refresh tokens and provider credentials live only in an encrypted
  rclone config outside the Git checkout.
- Keep the rclone-config encryption password in the macOS Login Keychain or in
  KeePassXC through auto-pass, and let rclone retrieve it through
  `--password-command`. Do not put it in an environment variable, command
  argument, tracked file, chat, or terminal log. The auto-pass helper writes
  the password only to stdout, which rclone consumes; its diagnostics and its
  `--check` mode never print the value.
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

## Choose where the config password lives

The rclone config encryption password is what keeps OAuth refresh tokens and
the iCloud app-specific password unreadable at rest. It is not itself a
provider credential. Store it in exactly one of:

- the macOS Login Keychain, the default on darwin; or
- KeePassXC through the `auto-pass` sibling repo, for owners who keep
  portfolio secrets in a vault.

Both are local-only. Pick one and keep a recovery copy of the password in the
portfolio password manager either way: losing it makes the encrypted rclone
config unrecoverable.

## Config password from KeePassXC (auto-pass)

Store the password in the vault, then point the repo at that entry. Create the
group first, because `keepassxc-cli add` will not create a missing parent:

```bash
keepassxc-cli mkdir "$KDBX" snowbridge
keepassxc-cli add --password-prompt "$KDBX" snowbridge/rclone-config
```

Copy the tracked example to the ignored `config/auto-pass.ini` and set the
entry path:

```ini
[auto_pass]
profile = snowbridge

[cloud]
rclone_config_keepass_entry = snowbridge/rclone-config
rclone_config_keepass_field = Password
```

Verify the lookup. `--check` prints only the resolved length, never the
password:

```bash
python3 scripts/rclone_config_password.py --check
```

Run that check from a terminal the first time. The database password is
prompted once and cached, which is what lets later non-interactive runs work.

Three prerequisites are easy to miss, because the doctor runs rclone in a
deliberately minimal environment and rclone spawns this helper beneath it:

- **No `AUTO_PASS_*` variable from your shell survives into the helper.** The
  profile named above must therefore supply the database path from
  `../auto-pass/config/auto-pass.env.local`. A profile whose database sits on
  an unmounted share cannot be opened.
- **The database password comes from the cache, not from that file.** Leave
  the profile's password empty and unlock once interactively. The unlocked
  password is cached at mode 0600 under `~/.config/snowbridge/cache`, an
  owner-only directory this helper creates and re-asserts the mode on.
  auto-pass would otherwise cache into the shared `~/.cache`, which other
  tools use and which is not writable on every host. The cache expires, so the
  prompt returns periodically; putting the database password in the env file
  instead would make it a permanent plaintext secret.
- **Python 3.11 or newer is required**, because that is what auto-pass
  declares. The minimal `PATH` places `/usr/bin` ahead of Homebrew, so a bare
  `python3` there is the macOS system Python 3.9. `--password-source
  auto-pass` therefore pins the interpreter running the tool; the helper
  refuses an older one with an explicit message rather than an import error.

Then run the doctor against that source:

```bash
python3 scripts/cloud_accounts.py doctor --password-source auto-pass
```

If the provider cannot produce the password, the doctor reports that
specifically rather than claiming the config is unencrypted.

## Create the macOS Keychain secret

Skip this section when using auto-pass. First verify that the fixed item does
not already exist:

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

With auto-pass, substitute the helper for the Keychain lookup. Pin the
interpreter, for the `PATH` reason described above:

```bash
install -d -m 0700 "$HOME/.config/snowbridge"
rclone \
  --config "$HOME/.config/snowbridge/rclone.conf" \
  --password-command "$(command -v python3) $PWD/scripts/rclone_config_password.py" \
  config encryption set
```

This encryption step is local-only.

## Online provider enrollment

Stop here until the provider, intended folder, and acceptable permission scope
have been chosen; see [Supported providers](#supported-providers) for the scope
each one accepts. Then start rclone's provider-specific flow from a private
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
The `backend` value is the exact rclone type (`drive`, `onedrive`, or
`iclouddrive` for the onboarded providers; other inventory-only types such as
`dropbox`, `s3`, or `sftp` remain accepted), not a display name. Keep
`enabled = false` until
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
