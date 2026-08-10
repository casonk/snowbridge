# Security Policy

## Reporting

Do not file sensitive disclosures in public issues.

Security-sensitive reports for this repository should be handled privately by
the repository owner because this repo may describe home-network topology,
device access patterns, and local storage layout.

## Scope

- Do not commit passwords, Samba passdb exports, TDB files, private keys,
  certificates, VPN secrets, or actual shared user data.
- Do not commit machine-specific hostnames, public endpoints, usernames, or
  local absolute paths unless an exact value is required for a safe template.
- Keep cloud OAuth tokens, refresh tokens, client secrets, account addresses,
  and the rclone config password out of Git, chat, shell arguments, and logs.
  The live rclone config must be encrypted and stored outside the checkout.
- Treat `CHATHISTORY.md` as local-only operational memory.
- Treat firewall state, VPN state, and service logs as sensitive operational
  context unless they are explicitly sanitized.

## Safe Operating Practices

- Keep guest access disabled unless the user explicitly chooses otherwise.
- Prefer a dedicated SMB account with the least privileges needed for the share.
- Keep the share root outside this git repository.
- Restrict SMB access to the home LAN or a private VPN overlay.
- Do not expose TCP 445 directly to the public internet.
- Do not enable cloud copy, sync, mount, or deletion behavior from account
  discovery alone. Inventory proof is not conflict resolution or an ACID
  transaction boundary.
- Treat any custom rclone config-password command as a separate trust and I/O
  boundary. The documented macOS default uses only the local Login Keychain.
